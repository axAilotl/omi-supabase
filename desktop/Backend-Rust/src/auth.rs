use axum::{
    async_trait,
    extract::FromRequestParts,
    http::{request::Parts, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

use crate::config::Config;

#[derive(Debug, Deserialize)]
pub struct SupabaseClaims {
    pub sub: String,
    pub aud: Option<String>,
    pub exp: u64,
    pub iat: u64,
    pub email: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct AuthError {
    pub error: String,
    pub message: String,
}

#[derive(Clone)]
struct CachedJwks {
    set: JwkSet,
    fetched_at: Instant,
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        (StatusCode::UNAUTHORIZED, Json(self)).into_response()
    }
}

pub struct SupabaseAuth {
    jwt_secret: Option<String>,
    jwks_url: Option<String>,
    audience: String,
    issuer: Option<String>,
    http_client: Client,
    cached_jwks: Arc<RwLock<Option<CachedJwks>>>,
}

impl SupabaseAuth {
    pub fn new(config: &Config) -> Self {
        let auth_url = config.supabase_auth_url.clone().or_else(|| {
            config
                .supabase_url
                .as_ref()
                .map(|base_url| format!("{}/auth/v1", base_url.trim_end_matches('/')))
        });
        let jwks_url = config.supabase_jwks_url.clone().or_else(|| {
            auth_url
                .as_ref()
                .map(|value| format!("{}/.well-known/jwks.json", value.trim_end_matches('/')))
        });

        Self {
            jwt_secret: config.supabase_jwt_secret.clone(),
            jwks_url,
            audience: config
                .supabase_jwt_audience
                .clone()
                .unwrap_or_else(|| "authenticated".to_string()),
            issuer: config.supabase_jwt_issuer.clone().or(auth_url),
            http_client: Client::new(),
            cached_jwks: Arc::new(RwLock::new(None)),
        }
    }

    async fn get_jwks(&self) -> Result<JwkSet, AuthError> {
        {
            let cache = self.cached_jwks.read().await;
            if let Some(cached) = cache.as_ref() {
                if cached.fetched_at.elapsed() < Duration::from_secs(300) {
                    return Ok(cached.set.clone());
                }
            }
        }

        let jwks_url = self.jwks_url.as_ref().ok_or_else(|| AuthError {
            error: "server_error".to_string(),
            message: "Supabase JWKS URL not configured".to_string(),
        })?;

        let response = self
            .http_client
            .get(jwks_url)
            .send()
            .await
            .map_err(|e| AuthError {
                error: "server_error".to_string(),
                message: format!("Failed to fetch JWKS: {}", e),
            })?;

        if !response.status().is_success() {
            return Err(AuthError {
                error: "server_error".to_string(),
                message: format!("Failed to fetch JWKS: HTTP {}", response.status()),
            });
        }

        let set = response.json::<JwkSet>().await.map_err(|e| AuthError {
            error: "server_error".to_string(),
            message: format!("Failed to decode JWKS: {}", e),
        })?;

        {
            let mut cache = self.cached_jwks.write().await;
            *cache = Some(CachedJwks {
                set: set.clone(),
                fetched_at: Instant::now(),
            });
        }

        Ok(set)
    }

    async fn resolve_decoding_key(
        &self,
        algorithm: Algorithm,
        kid: Option<&str>,
    ) -> Result<DecodingKey, AuthError> {
        match algorithm {
            Algorithm::HS256 | Algorithm::HS384 | Algorithm::HS512 => {
                let secret = self.jwt_secret.as_ref().ok_or_else(|| AuthError {
                    error: "server_error".to_string(),
                    message: "Supabase JWT secret not configured".to_string(),
                })?;
                Ok(DecodingKey::from_secret(secret.as_bytes()))
            }
            _ => {
                let kid = kid.ok_or_else(|| AuthError {
                    error: "invalid_token".to_string(),
                    message: "JWT header is missing kid".to_string(),
                })?;
                let jwks = self.get_jwks().await?;
                let jwk = jwks.find(kid).ok_or_else(|| AuthError {
                    error: "invalid_token".to_string(),
                    message: format!("No signing key found for kid {}", kid),
                })?;
                DecodingKey::from_jwk(jwk).map_err(|e| AuthError {
                    error: "invalid_token".to_string(),
                    message: format!("Failed to decode JWKS key: {}", e),
                })
            }
        }
    }

    fn validation_for_algorithm(&self, algorithm: Algorithm) -> Validation {
        let mut validation = Validation::new(algorithm);
        validation.set_audience(&[self.audience.as_str()]);
        if let Some(issuer) = &self.issuer {
            validation.set_issuer(&[issuer.as_str()]);
        }
        validation
    }

    pub async fn verify_token(
        &self,
        token: &str,
    ) -> Result<(String, Option<String>, Option<String>), AuthError> {
        let header = decode_header(token).map_err(|e| AuthError {
            error: "invalid_token".to_string(),
            message: format!("Invalid token header: {}", e),
        })?;
        let decoding_key = self
            .resolve_decoding_key(header.alg, header.kid.as_deref())
            .await?;
        let validation = self.validation_for_algorithm(header.alg);

        let token_data =
            decode::<SupabaseClaims>(token, &decoding_key, &validation).map_err(|e| AuthError {
                error: "invalid_token".to_string(),
                message: format!("Token validation failed: {}", e),
            })?;

        Ok((
            token_data.claims.sub,
            token_data.claims.name,
            token_data.claims.email,
        ))
    }
}

#[derive(Debug, Clone)]
pub struct AuthUser {
    pub uid: String,
    pub name: Option<String>,
    pub email: Option<String>,
}

#[derive(Clone)]
pub struct SupabaseAuthExt(pub Arc<SupabaseAuth>);

#[async_trait]
impl<S> FromRequestParts<S> for AuthUser
where
    S: Send + Sync,
{
    type Rejection = AuthError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let auth_header = parts
            .headers
            .get("Authorization")
            .and_then(|h| h.to_str().ok())
            .ok_or_else(|| AuthError {
                error: "missing_token".to_string(),
                message: "Authorization header required".to_string(),
            })?;

        let token = auth_header
            .strip_prefix("Bearer ")
            .ok_or_else(|| AuthError {
                error: "invalid_token".to_string(),
                message: "Invalid Authorization header format".to_string(),
            })?;

        let supabase_auth = parts
            .extensions
            .get::<SupabaseAuthExt>()
            .ok_or_else(|| AuthError {
                error: "server_error".to_string(),
                message: "Supabase auth not configured".to_string(),
            })?;

        let (uid, name, email) = supabase_auth.0.verify_token(token).await?;
        Ok(AuthUser { uid, name, email })
    }
}

pub fn supabase_auth_extension(auth: Arc<SupabaseAuth>) -> axum::Extension<SupabaseAuthExt> {
    axum::Extension(SupabaseAuthExt(auth))
}
