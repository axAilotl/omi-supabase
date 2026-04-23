create extension if not exists pgcrypto;

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

create table if not exists public.user_profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  data_protection_level text not null default 'enhanced',
  agent_vm jsonb,
  language text,
  onboarding jsonb not null default '{}'::jsonb,
  migration_status jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.chat_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text,
  preview text,
  message_count integer not null default 0,
  starred boolean not null default false,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  app_id text,
  plugin_id text,
  openai_thread_id text,
  openai_assistant_id text,
  message_ids jsonb not null default '[]'::jsonb,
  file_ids jsonb not null default '[]'::jsonb
);

create table if not exists public.chat_messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  chat_session_id uuid references public.chat_sessions(id) on delete set null,
  text text not null default '',
  created_at timestamptz not null default timezone('utc', now()),
  sender text not null,
  app_id text,
  plugin_id text,
  from_external_integration boolean not null default false,
  type text not null default 'text',
  memories_id jsonb not null default '[]'::jsonb,
  files_id jsonb not null default '[]'::jsonb,
  data_protection_level text,
  reported boolean not null default false,
  report_reason text,
  metadata text,
  rating integer,
  langsmith_run_id text,
  prompt_name text,
  prompt_commit text,
  chart_data jsonb
);

create index if not exists idx_chat_sessions_user_plugin_updated_at
  on public.chat_sessions (user_id, plugin_id, updated_at desc);

create index if not exists idx_chat_messages_user_session_created_at
  on public.chat_messages (user_id, chat_session_id, created_at desc);

create index if not exists idx_chat_messages_user_plugin_created_at
  on public.chat_messages (user_id, plugin_id, created_at desc);

drop trigger if exists trg_user_profiles_touch_updated_at on public.user_profiles;
create trigger trg_user_profiles_touch_updated_at
before update on public.user_profiles
for each row execute function public.touch_updated_at();

drop trigger if exists trg_chat_sessions_touch_updated_at on public.chat_sessions;
create trigger trg_chat_sessions_touch_updated_at
before update on public.chat_sessions
for each row execute function public.touch_updated_at();

alter table public.user_profiles enable row level security;
alter table public.chat_sessions enable row level security;
alter table public.chat_messages enable row level security;

drop policy if exists "user_profiles_self" on public.user_profiles;
create policy "user_profiles_self"
on public.user_profiles
for all
using (auth.uid() = id)
with check (auth.uid() = id);

drop policy if exists "chat_sessions_self" on public.chat_sessions;
create policy "chat_sessions_self"
on public.chat_sessions
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "chat_messages_self" on public.chat_messages;
create policy "chat_messages_self"
on public.chat_messages
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);
