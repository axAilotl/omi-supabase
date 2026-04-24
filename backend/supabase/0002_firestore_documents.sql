create table if not exists public.firestore_documents (
  path text primary key,
  collection_path text not null,
  collection_id text not null,
  doc_id text not null,
  parent_path text,
  root_uid text,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_firestore_documents_collection_path
  on public.firestore_documents (collection_path);

create index if not exists idx_firestore_documents_collection_id
  on public.firestore_documents (collection_id);

create index if not exists idx_firestore_documents_parent_path
  on public.firestore_documents (parent_path);

create index if not exists idx_firestore_documents_root_uid
  on public.firestore_documents (root_uid);

create index if not exists idx_firestore_documents_data_gin
  on public.firestore_documents
  using gin (data jsonb_path_ops);

drop trigger if exists trg_firestore_documents_touch_updated_at on public.firestore_documents;
create trigger trg_firestore_documents_touch_updated_at
before update on public.firestore_documents
for each row execute function public.touch_updated_at();
