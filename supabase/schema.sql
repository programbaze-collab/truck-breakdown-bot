-- Выполнить один раз в Supabase: SQL Editor -> New query -> Run.

create table if not exists drivers (
    id          bigint generated always as identity primary key,
    tg_user_id  bigint not null unique,
    tg_chat_id  bigint not null,
    full_name   text,
    phone       text,
    plate       text,
    -- шаг регистрации: name -> phone -> plate -> done
    reg_step    text not null default 'name'
                check (reg_step in ('name', 'phone', 'plate', 'done')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists requests (
    id                bigint generated always as identity primary key,
    driver_id         bigint not null references drivers(id) on delete cascade,
    -- исходное сообщение водителя: защита от повторной доставки
    source_chat_id    bigint not null,
    source_message_id bigint not null,
    text_hash         text   not null,
    raw_text          text   not null,

    category  text check (category in ('wheel', 'engine', 'brakes', 'electrics', 'accident', 'other')),
    urgency   text check (urgency in ('can_drive', 'need_help', 'stuck')),
    location  text,
    latitude  double precision,
    longitude double precision,
    cargo     text check (cargo in ('loaded', 'empty')),

    photos_done boolean not null default false,
    awaiting    text,  -- какой вопрос бот задал последним
    status      text not null default 'draft'
                check (status in ('draft', 'new', 'accepted', 'help_sent', 'cancelled')),

    dispatcher_chat_id    bigint,
    dispatcher_message_id bigint,
    submitted_at  timestamptz,
    accepted_by   text,
    accepted_at   timestamptz,
    help_sent_by  text,
    help_sent_at  timestamptz,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    unique (source_chat_id, source_message_id)
);

-- у водителя не больше одного черновика одновременно
create unique index if not exists requests_one_draft_per_driver
    on requests (driver_id) where status = 'draft';
create index if not exists requests_dup_lookup
    on requests (driver_id, text_hash, created_at);

create table if not exists request_photos (
    id                bigint generated always as identity primary key,
    request_id        bigint not null references requests(id) on delete cascade,
    tg_file_id        text   not null,
    tg_file_unique_id text   not null,
    kind              text   not null default 'photo' check (kind in ('photo', 'document')),
    url               text   not null,
    created_at        timestamptz not null default now(),
    unique (request_id, tg_file_unique_id)
);

-- RLS включён без политик: anon-ключом таблицы не прочитать,
-- бот ходит service_role ключом, который RLS обходит.
alter table drivers        enable row level security;
alter table requests       enable row level security;
alter table request_photos enable row level security;

-- явные права для серверного ключа: работает, даже если при создании проекта
-- не была отмечена опция "Grant API access to new tables automatically"
grant usage on schema public to service_role;
grant select, insert, update, delete on drivers, requests, request_photos to service_role;

-- публичный бакет для фото (ссылки открываются диспетчером без авторизации)
insert into storage.buckets (id, name, public)
values ('breakdown-photos', 'breakdown-photos', true)
on conflict (id) do nothing;
