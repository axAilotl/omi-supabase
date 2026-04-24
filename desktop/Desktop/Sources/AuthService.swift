import AppKit
import Foundation
import Sentry

extension Notification.Name {
    /// Posted by AuthService.signOut() so views can reset @AppStorage-backed properties directly.
    static let userDidSignOut = Notification.Name("com.omi.desktop.userDidSignOut")
}

@MainActor
class AuthService {
    static let shared = AuthService()

    private var authState: AuthState { AuthState.shared }

    var isSignedIn: Bool {
        get { authState.isSignedIn }
        set { authState.isSignedIn = newValue }
    }

    var isLoading: Bool {
        get { authState.isLoading }
        set { authState.isLoading = newValue }
    }

    var error: String? {
        get { authState.error }
        set { authState.error = newValue }
    }

    private var isConfigured = false
    private var oauthContinuation: CheckedContinuation<(code: String, state: String), Error>?

    private let apiBaseURL: String = {
        if let envURL = getenv("OMI_AUTH_URL") {
            let url = String(cString: envURL)
            if !url.isEmpty {
                return url.hasSuffix("/") ? url : url + "/"
            }
        }
        NSLog("OMI AUTH: OMI_AUTH_URL not set — OAuth sign-in will fail")
        return ""
    }()

    private var redirectURI: String {
        "\(urlScheme)://auth/callback"
    }

    private var currentBundleIdentifier: String {
        Bundle.main.bundleIdentifier ?? "unknown.bundle"
    }

    private var urlScheme: String {
        if let urlTypes = Bundle.main.infoDictionary?["CFBundleURLTypes"] as? [[String: Any]],
           let firstType = urlTypes.first,
           let schemes = firstType["CFBundleURLSchemes"] as? [String],
           let scheme = schemes.first {
            return scheme
        }
        return "omi-computer"
    }

    private let kAuthIsSignedIn = "auth_isSignedIn"
    private let kAuthUserEmail = "auth_userEmail"
    private let kAuthUserId = "auth_userId"
    private let kAuthGivenName = "auth_givenName"
    private let kAuthFamilyName = "auth_familyName"
    private let kAuthIdToken = "auth_idToken"
    private let kAuthRefreshToken = "auth_refreshToken"
    private let kAuthTokenExpiry = "auth_tokenExpiry"
    private let kAuthTokenUserId = "auth_tokenUserId"

    var givenName: String {
        get { UserDefaults.standard.string(forKey: kAuthGivenName) ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: kAuthGivenName) }
    }

    var familyName: String {
        get { UserDefaults.standard.string(forKey: kAuthFamilyName) ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: kAuthFamilyName) }
    }

    var displayName: String {
        let given = givenName
        let family = familyName
        if !given.isEmpty && !family.isEmpty {
            return "\(given) \(family)"
        }
        if !given.isEmpty {
            return given
        }
        if !family.isEmpty {
            return family
        }
        return ""
    }

    private init() {}

    func configure() {
        guard !isConfigured else { return }
        isConfigured = true
        restoreAuthState()

        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) {
            if AuthState.shared.isRestoringAuth {
                NSLog("OMI AUTH: Auth restore timed out after 5s, clearing loading state")
                AuthState.shared.isRestoringAuth = false
            }
        }
    }

    private func saveAuthState(isSignedIn: Bool, email: String?, userId: String?) {
        UserDefaults.standard.set(isSignedIn, forKey: kAuthIsSignedIn)
        UserDefaults.standard.set(email, forKey: kAuthUserEmail)
        UserDefaults.standard.set(userId, forKey: kAuthUserId)
        UserDefaults.standard.synchronize()
        AuthState.shared.userEmail = email
        NSLog("OMI AUTH: Saved auth state - signedIn: %@, email: %@", isSignedIn ? "true" : "false", email ?? "nil")
    }

    private func restoreAuthState() {
        let savedSignedIn = UserDefaults.standard.bool(forKey: kAuthIsSignedIn)
        let savedEmail = UserDefaults.standard.string(forKey: kAuthUserEmail)

        NSLog(
            "OMI AUTH: Checking saved auth state - savedSignedIn: %@, savedEmail: %@",
            savedSignedIn ? "true" : "false",
            savedEmail ?? "nil"
        )

        guard savedSignedIn else {
            AuthState.shared.isRestoringAuth = false
            return
        }

        if let savedUserId = UserDefaults.standard.string(forKey: kAuthUserId), savedUserId.isEmpty,
           let storedToken = storedIdToken,
           let payload = decodeJWT(storedToken),
           let userId = payload["sub"] as? String {
            UserDefaults.standard.set(userId, forKey: kAuthUserId)
            NSLog("OMI AUTH: Backfilled missing userId from stored JWT: %@", userId)
        }

        isSignedIn = true
        AuthState.shared.userEmail = savedEmail
        AuthState.shared.isRestoringAuth = false
        loadNameFromBackendIfNeeded()
    }

    @MainActor
    func signInWithApple() async throws {
        try await signIn(provider: "apple")
    }

    @MainActor
    func signInWithGoogle() async throws {
        try await signIn(provider: "google")
    }

    @MainActor
    private func signIn(provider: String) async throws {
        guard !isLoading else {
            NSLog("OMI AUTH: Sign in already in progress, ignoring duplicate request")
            return
        }

        NSLog("OMI AUTH: Starting %@ OAuth sign-in", provider)
        isLoading = true
        error = nil
        AnalyticsManager.shared.signInStarted(provider: provider)

        defer { isLoading = false }

        do {
            let state = generateState()
            let authURL = buildAuthorizationURL(provider: provider, state: state)

            guard let url = URL(string: authURL) else {
                throw AuthError.invalidURL
            }

            NSWorkspace.shared.open(url)
            let (code, returnedState) = try await waitForOAuthCallback()
            guard returnedState == state else {
                throw AuthError.stateMismatch
            }

            let session = try await exchangeCodeForSession(code: code)

            givenName = session.givenName ?? ""
            familyName = session.familyName ?? ""
            AuthState.shared.userEmail = session.email

            saveTokens(
                idToken: session.accessToken,
                refreshToken: session.refreshToken,
                expiresIn: session.expiresIn,
                userId: session.userId
            )
            saveAuthState(isSignedIn: true, email: session.email, userId: session.userId)
            isSignedIn = true

            await RewindDatabase.shared.configure(userId: session.userId)
            if givenName.isEmpty {
                loadNameFromBackendIfNeeded()
            }

            AnalyticsManager.shared.identify()
            AnalyticsManager.shared.signInCompleted(provider: provider)
            APIKeyService.shared.startFetchingKeys()
            Task { await SettingsSyncManager.shared.syncFromServer() }

            if !AnalyticsManager.isDevBuild {
                let sentryUser = Sentry.User(userId: session.userId)
                sentryUser.email = session.email
                sentryUser.username = displayName.isEmpty ? nil : displayName
                SentrySDK.setUser(sentryUser)
            }

            fetchConversations()
        } catch AuthError.cancelled {
            NSLog("OMI AUTH: %@ sign-in cancelled", provider)
            self.error = nil
            throw AuthError.cancelled
        } catch {
            let nsError = error as NSError
            NSLog(
                "OMI AUTH: %@ sign-in failed (domain=%@ code=%d): %@",
                provider,
                nsError.domain,
                nsError.code,
                nsError.localizedDescription
            )
            logError("AUTH: \(provider) sign-in failed", error: error)
            AnalyticsManager.shared.signInFailed(provider: provider, error: error.localizedDescription)
            self.error = error.localizedDescription
            throw error
        }
    }

    private func buildAuthorizationURL(provider: String, state: String) -> String {
        let encodedRedirectURI = redirectURI.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? redirectURI
        return "\(apiBaseURL)v1/auth/authorize?provider=\(provider)&redirect_uri=\(encodedRedirectURI)&state=\(state)"
    }

    private func waitForOAuthCallback() async throws -> (code: String, state: String) {
        try await withCheckedThrowingContinuation { continuation in
            self.oauthContinuation = continuation

            Task {
                try await Task.sleep(nanoseconds: 5 * 60 * 1_000_000_000)
                if self.oauthContinuation != nil {
                    self.oauthContinuation?.resume(throwing: AuthError.timeout)
                    self.oauthContinuation = nil
                }
            }
        }
    }

    @MainActor
    func handleOAuthCallback(url: URL) {
        NSLog("OMI AUTH: Received OAuth callback: %@", url.absoluteString)

        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            oauthContinuation?.resume(throwing: AuthError.invalidCallback)
            oauthContinuation = nil
            return
        }

        guard url.scheme == urlScheme, url.host == "auth", url.path == "/callback" else {
            return
        }

        let queryItems = components.queryItems ?? []
        let code = queryItems.first(where: { $0.name == "code" })?.value
        let state = queryItems.first(where: { $0.name == "state" })?.value
        let error = queryItems.first(where: { $0.name == "error" })?.value

        if let state, let targetBundleId = targetBundleIdentifier(from: state), targetBundleId != currentBundleIdentifier {
            forwardOAuthCallback(url: url, toBundleId: targetBundleId)
            return
        }

        if let error {
            oauthContinuation?.resume(throwing: AuthError.oauthError(error))
            oauthContinuation = nil
            return
        }

        guard let code, let state else {
            oauthContinuation?.resume(throwing: AuthError.missingCodeOrState)
            oauthContinuation = nil
            return
        }

        oauthContinuation?.resume(returning: (code: code, state: state))
        oauthContinuation = nil
    }

    @MainActor
    func cancelSignIn() {
        guard oauthContinuation != nil else {
            isLoading = false
            return
        }
        oauthContinuation?.resume(throwing: AuthError.cancelled)
        oauthContinuation = nil
    }

    private struct SessionExchangeResult {
        let accessToken: String
        let refreshToken: String
        let expiresIn: Int
        let userId: String
        let email: String?
        let givenName: String?
        let familyName: String?
    }

    private func exchangeCodeForSession(code: String) async throws -> SessionExchangeResult {
        guard let url = URL(string: "\(apiBaseURL)v1/auth/token") else {
            throw AuthError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")

        let bodyParams = [
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirectURI,
        ]
        let bodyString = bodyParams
            .map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? $0.value)" }
            .joined(separator: "&")
        request.httpBody = bodyString.data(using: .utf8)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw AuthError.invalidResponse
        }
        guard httpResponse.statusCode == 200 else {
            let responseBody = String(data: data, encoding: .utf8) ?? "unknown"
            NSLog("OMI AUTH: Session exchange failed: %@", responseBody)
            throw AuthError.tokenExchangeFailed(httpResponse.statusCode)
        }
        return try parseSessionResponse(data: data)
    }

    private func parseSessionResponse(data: Data) throws -> SessionExchangeResult {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw AuthError.invalidResponse
        }

        guard let accessToken = json["access_token"] as? String,
              let refreshToken = json["refresh_token"] as? String else {
            throw AuthError.missingAccessToken
        }

        let expiresIn = coerceInt(json["expires_in"], fallback: 3600)
        let user = json["user"] as? [String: Any]
        let userMetadata = user?["user_metadata"] as? [String: Any]
        let providerIdToken = json["id_token"] as? String

        var userId = user?["id"] as? String ?? ""
        var email = user?["email"] as? String
        var givenName = userMetadata?["given_name"] as? String
        var familyName = userMetadata?["family_name"] as? String

        if let fullName = userMetadata?["full_name"] as? String ?? userMetadata?["name"] as? String,
           givenName == nil {
            let parts = fullName.split(separator: " ", maxSplits: 1).map(String.init)
            givenName = parts.first
            familyName = parts.count > 1 ? parts[1] : familyName
        }

        if let payload = decodeJWT(accessToken) {
            if userId.isEmpty {
                userId = payload["sub"] as? String ?? ""
            }
            if email == nil {
                email = payload["email"] as? String
            }
        }

        if let providerIdToken,
           let providerPayload = decodeJWT(providerIdToken) {
            if email == nil {
                email = providerPayload["email"] as? String
            }
            if givenName == nil {
                givenName = providerPayload["given_name"] as? String
            }
            if familyName == nil {
                familyName = providerPayload["family_name"] as? String
            }
            if givenName == nil,
               let name = providerPayload["name"] as? String {
                let parts = name.split(separator: " ", maxSplits: 1).map(String.init)
                givenName = parts.first
                familyName = parts.count > 1 ? parts[1] : familyName
            }
        }

        if userId.isEmpty {
            throw AuthError.invalidResponse
        }

        return SessionExchangeResult(
            accessToken: accessToken,
            refreshToken: refreshToken,
            expiresIn: expiresIn,
            userId: userId,
            email: email,
            givenName: givenName,
            familyName: familyName
        )
    }

    private func decodeJWT(_ jwt: String) -> [String: Any]? {
        let parts = jwt.split(separator: ".")
        guard parts.count >= 2 else { return nil }

        var base64 = String(parts[1])
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while base64.count % 4 != 0 {
            base64 += "="
        }

        guard let data = Data(base64Encoded: base64),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return json
    }

    @MainActor
    func updateGivenName(_ fullName: String) async {
        let trimmedName = fullName.trimmingCharacters(in: .whitespaces)
        let nameParts = trimmedName.split(separator: " ", maxSplits: 1)
        let newGivenName = nameParts.first.map(String.init) ?? trimmedName
        let newFamilyName = nameParts.count > 1 ? String(nameParts[1]) : ""

        givenName = newGivenName
        familyName = newFamilyName

        let isImpersonating = UserDefaults.standard.bool(forKey: "auth_isImpersonating")
        guard !isImpersonating else { return }

        do {
            try await APIClient.shared.updateUserProfile(name: trimmedName)
        } catch {
            NSLog("OMI AUTH: Failed to update backend profile name (non-fatal): %@", error.localizedDescription)
        }
    }

    func loadNameFromBackendIfNeeded() {
        guard givenName.isEmpty else { return }
        Task {
            do {
                let profile = try await APIClient.shared.getUserProfile()
                if let name = profile.name, !name.trimmingCharacters(in: .whitespaces).isEmpty {
                    let trimmed = name.trimmingCharacters(in: .whitespaces)
                    let nameParts = trimmed.split(separator: " ", maxSplits: 1)
                    await MainActor.run {
                        self.givenName = nameParts.first.map(String.init) ?? trimmed
                        self.familyName = nameParts.count > 1 ? String(nameParts[1]) : ""
                    }
                }
            } catch {
                NSLog("OMI AUTH: Failed to fetch backend profile for name (non-fatal): %@", error.localizedDescription)
            }
        }
    }

    private func saveTokens(idToken: String, refreshToken: String, expiresIn: Int, userId: String) {
        UserDefaults.standard.set(idToken, forKey: kAuthIdToken)
        UserDefaults.standard.set(refreshToken, forKey: kAuthRefreshToken)
        let expiryTime = Date().addingTimeInterval(TimeInterval(max(expiresIn - 300, 60)))
        UserDefaults.standard.set(expiryTime.timeIntervalSince1970, forKey: kAuthTokenExpiry)
        UserDefaults.standard.set(userId, forKey: kAuthTokenUserId)
        NSLog("OMI AUTH: Saved Supabase session for user %@", userId)
    }

    private func clearTokens() {
        UserDefaults.standard.removeObject(forKey: kAuthIdToken)
        UserDefaults.standard.removeObject(forKey: kAuthRefreshToken)
        UserDefaults.standard.removeObject(forKey: kAuthTokenExpiry)
        UserDefaults.standard.removeObject(forKey: kAuthTokenUserId)
    }

    private func clearProfileCache() {
        UserDefaults.standard.removeObject(forKey: kAuthGivenName)
        UserDefaults.standard.removeObject(forKey: kAuthFamilyName)
        AuthState.shared.userEmail = nil
    }

    private var storedIdToken: String? {
        UserDefaults.standard.string(forKey: kAuthIdToken)
    }

    private var storedRefreshToken: String? {
        UserDefaults.standard.string(forKey: kAuthRefreshToken)
    }

    private var storedTokenUserId: String? {
        UserDefaults.standard.string(forKey: kAuthTokenUserId)
    }

    private var isTokenExpired: Bool {
        let expiryTime = UserDefaults.standard.double(forKey: kAuthTokenExpiry)
        guard expiryTime > 0 else { return true }
        return Date().timeIntervalSince1970 > expiryTime
    }

    private func refreshIdToken() async throws -> String {
        guard let refreshToken = storedRefreshToken else {
            throw AuthError.notSignedIn
        }
        guard let url = URL(string: "\(apiBaseURL)v1/auth/refresh") else {
            throw AuthError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let encodedRefreshToken = refreshToken.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? refreshToken
        request.httpBody = "refresh_token=\(encodedRefreshToken)".data(using: .utf8)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw AuthError.invalidResponse
        }
        guard httpResponse.statusCode == 200 else {
            let errorBody = String(data: data, encoding: .utf8) ?? "unknown"
            NSLog("OMI AUTH: Refresh failed (HTTP %d): %@", httpResponse.statusCode, errorBody)
            clearTokens()
            clearProfileCache()
            isSignedIn = false
            saveAuthState(isSignedIn: false, email: nil, userId: nil)
            throw AuthError.notSignedIn
        }

        let session = try parseSessionResponse(data: data)
        saveTokens(
            idToken: session.accessToken,
            refreshToken: session.refreshToken,
            expiresIn: session.expiresIn,
            userId: session.userId
        )
        if let email = session.email {
            AuthState.shared.userEmail = email
        }
        if givenName.isEmpty, let sessionGivenName = session.givenName {
            givenName = sessionGivenName
            familyName = session.familyName ?? ""
        }
        return session.accessToken
    }

    func getIdToken(forceRefresh: Bool = false) async throws -> String {
        let expectedUserId = UserDefaults.standard.string(forKey: kAuthUserId)

        if !forceRefresh, let token = storedIdToken, !isTokenExpired {
            if let tokenUserId = storedTokenUserId {
                if expectedUserId == nil || tokenUserId == expectedUserId {
                    if expectedUserId == nil {
                        UserDefaults.standard.set(tokenUserId, forKey: kAuthUserId)
                    }
                    return token
                }
                clearTokens()
            } else {
                return token
            }
        }

        if storedRefreshToken != nil {
            return try await refreshIdToken()
        }

        throw AuthError.notSignedIn
    }

    func getAuthHeader() async throws -> String {
        let token = try await getIdToken(forceRefresh: false)
        return "Bearer \(token)"
    }

    func fetchConversations() {
        Task {
            do {
                let conversations = try await APIClient.shared.getConversations(limit: 10)
                log("Fetched \(conversations.count) conversations")
            } catch {
                logError("Failed to fetch conversations", error: error)
            }
        }
    }

    func signOut() throws {
        AnalyticsManager.shared.signedOut()
        AnalyticsManager.shared.reset()

        if !AnalyticsManager.isDevBuild {
            SentrySDK.setUser(nil)
        }

        isSignedIn = false
        APIKeyService.shared.clear()
        saveAuthState(isSignedIn: false, email: nil, userId: nil)
        clearTokens()
        clearProfileCache()

        Task {
            await AgentSyncService.shared.stop()
            await FloatingBarUsageLimiter.shared.reset()
        }

        let closeGeneration = RewindDatabase.configureGeneration
        Task {
            await RewindDatabase.shared.closeIfStale(generation: closeGeneration)
            await RewindIndexer.shared.reset()
            await RewindStorage.shared.reset()
            await TranscriptionStorage.shared.invalidateCache()
            await MemoryStorage.shared.invalidateCache()
            await ActionItemStorage.shared.invalidateCache()
            await ProactiveStorage.shared.invalidateCache()
            await NoteStorage.shared.invalidateCache()
            await AIUserProfileService.shared.invalidateCache()
        }

        NotificationCenter.default.post(name: .userDidSignOut, object: nil)

        UserDefaults.standard.removeObject(forKey: "onboardingStep")
        UserDefaults.standard.removeObject(forKey: "hasTriggeredNotification")
        UserDefaults.standard.removeObject(forKey: "hasTriggeredAutomation")
        UserDefaults.standard.removeObject(forKey: "hasTriggeredScreenRecording")
        UserDefaults.standard.removeObject(forKey: "hasTriggeredMicrophone")
        UserDefaults.standard.removeObject(forKey: "hasTriggeredSystemAudio")
        UserDefaults.standard.removeObject(forKey: "onboardingChatMessages")
        UserDefaults.standard.removeObject(forKey: "onboardingACPSessionId")
        UserDefaults.standard.removeObject(forKey: "onboardingJustCompleted")
        UserDefaults.standard.removeObject(forKey: "transcriptionEnabled")

        NSLog("OMI AUTH: Signed out and cleared saved state")
    }

    private func generateState() -> String {
        UUID().uuidString + "|" + currentBundleIdentifier
    }

    private func targetBundleIdentifier(from state: String) -> String? {
        let parts = state.split(separator: "|", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return nil }
        let bundleId = parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
        return bundleId.isEmpty ? nil : bundleId
    }

    private func forwardOAuthCallback(url: URL, toBundleId bundleId: String) {
        guard let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) else {
            NSLog("OMI AUTH: Unable to forward callback. Bundle %@ not found.", bundleId)
            return
        }

        let config = NSWorkspace.OpenConfiguration()
        config.activates = true

        NSWorkspace.shared.open([url], withApplicationAt: appURL, configuration: config) { _, openError in
            if let openError {
                NSLog("OMI AUTH: Failed to forward callback to %@: %@", bundleId, openError.localizedDescription)
            }
        }
    }

    private func coerceInt(_ value: Any?, fallback: Int) -> Int {
        if let intValue = value as? Int {
            return intValue
        }
        if let doubleValue = value as? Double {
            return Int(doubleValue)
        }
        if let stringValue = value as? String, let parsed = Int(stringValue) {
            return parsed
        }
        return fallback
    }
}

enum AuthError: LocalizedError {
    case notSignedIn
    case invalidURL
    case stateMismatch
    case timeout
    case invalidCallback
    case oauthError(String)
    case missingCodeOrState
    case invalidResponse
    case tokenExchangeFailed(Int)
    case missingAccessToken
    case cancelled

    var errorDescription: String? {
        switch self {
        case .notSignedIn:
            return "User is not signed in"
        case .invalidURL:
            return "Invalid authentication URL"
        case .stateMismatch:
            return "Security state mismatch - please try again"
        case .timeout:
            return "Authentication timed out - please try again"
        case .invalidCallback:
            return "Invalid authentication callback"
        case .oauthError(let error):
            return "Authentication error: \(error)"
        case .missingCodeOrState:
            return "Missing authentication code"
        case .invalidResponse:
            return "Invalid server response"
        case .tokenExchangeFailed(let code):
            return "Token exchange failed with status \(code)"
        case .missingAccessToken:
            return "Server did not return a session token"
        case .cancelled:
            return "Sign in cancelled"
        }
    }
}
