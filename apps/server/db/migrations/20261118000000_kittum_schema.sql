-- migrate:up

-- Kittum's schema. The foundation tables every module shares come first: projects, actors, the
-- audit ledger, command receipts, and the job table. Each module's tables follow in a section of
-- their own.

create table project (
    id uuid primary key default uuidv7(),
    name text not null,
    policy_version text not null,
    created_at timestamptz not null default now()
);

create table actor (
    id uuid primary key default uuidv7(),
    identity_ref text not null unique,
    kind text not null check (kind in ('human', 'model', 'system')),
    organization text
);

create table audit_event (
    project_id uuid not null references project (id),
    seq bigint not null check (seq > 0),
    event_id uuid not null unique,
    type text not null,
    schema_version integer not null,
    actor_id uuid not null references actor (id),
    recorded_at timestamptz not null,
    payload jsonb not null,
    payload_hash char(64) not null,
    prev_hash char(64) not null,
    event_hash char(64) not null unique,
    primary key (project_id, seq)
);

create function audit_event_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_event is append-only (% on seq %)', tg_op, old.seq;
end;
$$;

create trigger audit_event_append_only
    before update or delete on audit_event
    for each row execute function audit_event_is_append_only();

create trigger audit_event_no_truncate
    before truncate on audit_event
    for each statement execute function audit_event_is_append_only();

create table checkpoint (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    seq bigint not null,
    head_hash char(64) not null,
    signature bytea not null,
    key_id text not null,
    tsa_token_hash char(64),
    created_at timestamptz not null default now(),
    foreign key (project_id, seq) references audit_event (project_id, seq)
);

create table command_receipt (
    command_id uuid primary key,
    project_id uuid not null references project (id),
    type text not null,
    accepted_at timestamptz not null default now(),
    first_seq bigint,
    last_seq bigint
);

create table job (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    kind text not null,
    stage_key text not null unique,
    payload jsonb not null,
    status text not null default 'queued'
        check (status in ('queued', 'running', 'succeeded', 'failed', 'dead')),
    attempt integer not null default 0,
    run_after timestamptz not null default now(),
    lease_owner text,
    lease_until timestamptz,
    output_hash char(64),
    last_error text,
    created_at timestamptz not null default now()
);

create index job_claimable on job (kind, run_after) where status = 'queued';

-- ---------------------------------------------------------------------------------------------
-- gateway

-- Gateway bookkeeping: every outbound attempt, the content-addressed response cache, the daily
-- request budgets, and the cooldowns. The rate-limit buckets themselves are created by
-- pyrate-limiter on first use, one table per source, so their shape stays that library's business.

create table gateway_attempt (
    id uuid primary key default uuidv7(),
    source text not null,
    request_hash char(64) not null,
    attempt integer not null check (attempt > 0),
    status integer check (status between 100 and 599),
    duration_ms integer not null check (duration_ms >= 0),
    error text,
    -- Each redirect followed, the hop's URL as built, without private values.
    redirects text[] not null,
    recorded_at timestamptz not null
);

create index gateway_attempt_by_source on gateway_attempt (source, recorded_at desc);
create index gateway_attempt_by_request on gateway_attempt (request_hash);

-- The body lives in the artifact store under `body_hash`; this table only indexes it.
create table gateway_cache (
    request_hash char(64) primary key,
    source text not null,
    status integer not null check (status between 100 and 599),
    headers jsonb not null,
    body_hash char(64) not null,
    stored_at timestamptz not null
);

create table gateway_budget (
    source text not null,
    day date not null,
    used integer not null check (used >= 0),
    primary key (source, day)
);

-- A `Retry-After` or an exhausted rate-limit window pauses a source for every worker, not only
-- the one that saw the header.
create table gateway_cooldown (
    source text primary key,
    until timestamptz not null
);

-- ---------------------------------------------------------------------------------------------
-- search

create table search_execution (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    topic_id text not null,
    source text not null,
    platform text not null,
    kind text not null check (kind in ('live_api', 'file_import')),
    strategy text not null,
    limits jsonb not null default '[]'::jsonb,
    query_translation text,
    reported_count integer not null,
    retrieved_count integer not null,
    started_at timestamptz not null,
    ended_at timestamptz not null,
    raw_artifact_hashes jsonb not null default '[]'::jsonb,
    skipped_record_ids jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    unique (id, project_id)
);

create index search_execution_project on search_execution (project_id, topic_id);

-- `pmcid` is the PubMed Central ID Europe PMC and PubMed name; deduplication matches on it.
create table source_occurrence (
    occurrence_id uuid primary key default uuidv7(),
    execution_id uuid not null,
    project_id uuid not null references project (id),
    source text not null,
    provider_record_id text not null,
    ordinal integer not null,
    title text not null,
    abstract text,
    authors jsonb not null default '[]'::jsonb,
    year integer,
    journal text,
    doi text,
    pmid text,
    registry_ids jsonb not null default '[]'::jsonb,
    publication_types jsonb not null default '[]'::jsonb,
    raw_hash char(64) not null,
    created_at timestamptz not null default now(),
    pmcid text,
    -- An occurrence belongs to the same project as its execution.
    foreign key (execution_id, project_id)
        references search_execution (id, project_id) on delete cascade
);

create index source_occurrence_execution on source_occurrence (execution_id);
create index source_occurrence_project on source_occurrence (project_id, source);
create index source_occurrence_pmid on source_occurrence (project_id, pmid) where pmid is not null;
create index source_occurrence_doi on source_occurrence (project_id, doi) where doi is not null;
create index source_occurrence_pmcid on source_occurrence (project_id, pmcid) where pmcid is not null;

-- ---------------------------------------------------------------------------------------------
-- corpus

create table report (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    created_at timestamptz not null default now()
);

create table study (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    created_at timestamptz not null default now()
);

create table corpus_revision (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    created_at timestamptz not null default now()
);

create table identity_revision (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    parent_id uuid references identity_revision (id),
    event_seq bigint not null default 0,
    event_id uuid,
    finalized boolean not null default false,
    created_at timestamptz not null default now()
);

create table identity_link (
    id uuid primary key default uuidv7(),
    revision_id uuid not null references identity_revision (id),
    occurrence_id uuid not null,
    report_id uuid not null references report (id),
    study_id uuid not null references study (id),
    relation text not null default 'standalone' check (relation in ('same_record', 'same_report', 'same_study', 'standalone')),
    source text not null default ''
);

create index idx_identity_link_revision on identity_link (revision_id);
create index idx_identity_link_occurrence on identity_link (occurrence_id);
create index idx_identity_link_report on identity_link (report_id);
create index idx_identity_link_study on identity_link (study_id);

-- A revision is written in one transaction with all its links and then marked finalized. From
-- then on neither the revision nor its links change, so PRISMA counts from it stay reproducible.
create function identity_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception '% is append-only (%)', tg_table_name, tg_op;
end;
$$;

create function identity_revision_only_finalizes() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE' and not old.finalized and new.finalized
        and (new.id, new.project_id, new.parent_id, new.event_seq, new.event_id, new.created_at)
            is not distinct from
            (old.id, old.project_id, old.parent_id, old.event_seq, old.event_id, old.created_at)
    then
        return new;
    end if;
    raise exception 'identity_revision % is immutable (%)', old.id, tg_op;
end;
$$;

create function identity_revision_is_finalized() returns trigger
language plpgsql as $$
begin
    if not exists (select 1 from identity_revision where id = new.id and finalized) then
        raise exception 'identity_revision % was not finalized in its transaction', new.id;
    end if;
    return null;
end;
$$;

create function identity_link_revision_is_open() returns trigger
language plpgsql as $$
begin
    if not exists (
        select 1 from identity_revision where id = new.revision_id and not finalized
    ) then
        raise exception 'identity_revision % is finalized or not visible', new.revision_id;
    end if;
    return new;
end;
$$;

create trigger identity_revision_frozen
    before update or delete on identity_revision
    for each row execute function identity_revision_only_finalizes();

create trigger identity_revision_no_truncate
    before truncate on identity_revision
    for each statement execute function identity_is_append_only();

create constraint trigger identity_revision_finalized
    after insert on identity_revision
    deferrable initially deferred
    for each row execute function identity_revision_is_finalized();

create trigger identity_link_frozen
    before update or delete on identity_link
    for each row execute function identity_is_append_only();

create trigger identity_link_no_truncate
    before truncate on identity_link
    for each statement execute function identity_is_append_only();

create trigger identity_link_open_revision
    before insert on identity_link
    for each row execute function identity_link_revision_is_open();

-- `exact`: a duplicate on a report identifier alone (DOI, PMID, PMCID), which the lead may
-- confirm with all the others in one step.
create table proposal (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    corpus_revision_id uuid references corpus_revision (id),
    relation text not null check (relation in ('same_record', 'same_report', 'same_study')),
    occurrence_ids uuid[] not null,
    evidence text[] not null,
    status text not null default 'pending' check (status in ('pending', 'confirmed', 'rejected')),
    created_at timestamptz not null default now(),
    exact boolean not null default false
);

create index idx_proposal_project on proposal (project_id);
create index idx_proposal_corpus_revision on proposal (corpus_revision_id);

-- A known pool: the papers a search topic is expected to find, such as the ones the review
-- team already knows, imported from their files. It is compared with the topic's searches
-- and never counts as a search result. Each file is one import; a withdrawn file stops counting but stays.
create table known_pool_file (
    id uuid primary key,
    project_id uuid not null references project (id),
    topic_id text not null,
    format text not null,
    file_name text,
    file_hash char(64) not null,
    entries integer not null check (entries > 0),
    imported_by uuid not null references actor (id),
    imported_event_id uuid not null,
    imported_at timestamptz not null,
    withdrawn_by uuid references actor (id),
    withdrawn_event_id uuid,
    withdrawn_at timestamptz,
    check ((withdrawn_event_id is null) = (withdrawn_at is null))
);

create index known_pool_file_topic on known_pool_file (project_id, topic_id);

-- One paper of a pool file as the file gives it (a search module `SourceRecord`).
create table known_pool_entry (
    id uuid primary key,
    file_id uuid not null references known_pool_file (id),
    ordinal integer not null,
    record jsonb not null,
    unique (file_id, ordinal)
);

-- ---------------------------------------------------------------------------------------------
-- documents

-- Documents, acquisitions, parsed layers, anchors, and anchor mappings (DESIGN.md, "Provenance
-- that survives re-parsing"). PDF bytes and layer JSON live in the artifact store; rows hold
-- their SHA-256. Layers and anchors never change once written.

create table document (
    id uuid primary key,
    artifact_hash char(64) not null unique,
    media_type text not null,
    byte_size bigint not null check (byte_size > 0),
    page_count integer check (page_count >= 0),
    created_at timestamptz not null default now()
);

create table acquisition (
    id uuid primary key,
    project_id uuid not null references project (id),
    document_id uuid not null references document (id),
    source text not null,
    url text,
    licence text,
    terms_url text,
    acquired_at timestamptz not null,
    event_id uuid not null
);

create index acquisition_by_document on acquisition (document_id);

create table layer (
    id uuid primary key,
    document_id uuid not null references document (id),
    recipe_hash char(64) not null,
    recipe jsonb not null,
    output_hash char(64) not null,
    text_hash char(64) not null,
    page_count integer not null,
    created_at timestamptz not null default now(),
    unique (document_id, recipe_hash)
);

create table anchor (
    id uuid primary key,
    layer_id uuid not null references layer (id),
    document_id uuid not null references document (id),
    quote jsonb not null,
    text_hash char(64) not null,
    start_offset integer not null check (start_offset >= 0),
    end_offset integer not null check (end_offset > start_offset),
    boxes jsonb not null,
    cells jsonb not null,
    created_at timestamptz not null default now()
);

create index anchor_by_layer on anchor (layer_id);

create table anchor_mapping (
    id uuid primary key,
    old_anchor_id uuid not null references anchor (id),
    layer_id uuid not null references layer (id),
    new_anchor_id uuid references anchor (id),
    status text not null
        check (status in ('exact', 'candidate', 'ambiguous', 'unresolved', 'reviewer_confirmed')),
    method text not null,
    method_version text not null,
    alternative_anchor_ids uuid[] not null default '{}',
    reviewed_event_id uuid,
    created_at timestamptz not null default now(),
    unique (old_anchor_id, layer_id, method_version),
    check ((status in ('exact', 'candidate')) = (new_anchor_id is not null))
);

create table page_render (
    document_id uuid not null references document (id),
    page integer not null check (page > 0),
    scale numeric(4, 2) not null check (scale > 0),
    renderer text not null,
    artifact_hash char(64) not null,
    primary key (document_id, page, scale, renderer)
);

create function documents_immutable() returns trigger
language plpgsql as $$
begin
    raise exception '% rows are immutable (%)', tg_table_name, tg_op;
end;
$$;

create trigger layer_immutable before update or delete on layer
    for each row execute function documents_immutable();

create trigger anchor_immutable before update or delete on anchor
    for each row execute function documents_immutable();

-- ---------------------------------------------------------------------------------------------
-- enrich

-- One enrichment per source occurrence. Re-enrichment after an update search replaces the row.
-- Enrichment never adds PRISMA identification counts; it only fills fields on existing records.

create table record_enrichment (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    occurrence_id uuid not null,
    provider_record_id text not null,
    doi text,
    pmid text,
    pmcid text,
    abstract text,
    retracted boolean not null default false,
    retraction_notice_doi text,
    oa_locations jsonb not null default '[]'::jsonb,
    entities jsonb not null default '[]'::jsonb,
    updates jsonb not null default '[]'::jsonb,
    clinical_trial_numbers jsonb not null default '[]'::jsonb,
    field_sources jsonb not null default '[]'::jsonb,
    enriched_at timestamptz not null default now(),
    unique (project_id, occurrence_id)
);

create index record_enrichment_doi on record_enrichment (project_id, doi);
create index record_enrichment_pmid on record_enrichment (project_id, pmid);

-- ---------------------------------------------------------------------------------------------
-- judge

-- The AI judge: its configurations and the verdicts it recorded. A verdict is never
-- changed; a new model, prompt, or protocol gives a new cache key and a new row.

create table judge_config (
    id text primary key,
    model_recipe_id text not null,
    model_recipe jsonb not null,
    weights_hash char(64) not null,
    prompt_version text not null,
    prompt_hash char(64) not null,
    schema_hash char(64) not null,
    decoding jsonb not null,
    created_at timestamptz not null
);

create table verdict (
    id uuid primary key,
    project_id uuid not null references project (id),
    judge_config_id text not null references judge_config (id),
    cache_key char(64) not null,
    item_ref text not null,
    scope_id text not null,
    stage text not null check (stage in ('title_abstract', 'full_text')),
    input_hash char(64) not null,
    protocol_hash char(64) not null,
    rendered_prompt_hash char(64) not null,
    schema_hash char(64) not null,
    criteria jsonb not null,
    excludes boolean not null,
    flags text[] not null,
    model_identity text,
    raw_output_hash char(64) not null,
    attempts jsonb not null,
    created_at timestamptz not null,
    unique (project_id, cache_key)
);

create index verdict_item on verdict (project_id, item_ref, scope_id);

create function verdict_is_immutable() returns trigger
language plpgsql as $$
begin
    raise exception 'verdict rows are immutable (% on %)', tg_op, old.id;
end;
$$;

create trigger verdict_immutable
    before update on verdict
    for each row execute function verdict_is_immutable();

-- ---------------------------------------------------------------------------------------------
-- protocol

-- Protocol versions. A version is saved as a draft and frozen when published. The
-- document and its hash never change; publishing only fills in the approval.

create table protocol_version (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    parent_id uuid references protocol_version (id),
    version_label text not null,
    content_hash char(64) not null,
    document jsonb not null,
    status text not null default 'draft' check (status in ('draft', 'published')),
    created_at timestamptz not null default now(),
    published_at timestamptz,
    approved_by uuid references actor (id),
    approved_event_id uuid references audit_event (event_id),
    publish_reason text,
    unique (project_id, content_hash),
    check (
        (status = 'draft' and published_at is null and approved_by is null
            and approved_event_id is null)
        or (status = 'published' and published_at is not null and approved_by is not null
            and approved_event_id is not null)
    )
);

create index protocol_version_by_project on protocol_version (project_id, created_at);

create function protocol_version_is_frozen() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        if old.status = 'published' then
            raise exception 'protocol_version % is published and frozen', old.id;
        end if;
        return old;
    end if;
    if old.status = 'published' then
        raise exception 'protocol_version % is published and frozen', old.id;
    end if;
    if new.project_id <> old.project_id
        or new.parent_id is distinct from old.parent_id
        or new.version_label <> old.version_label
        or new.content_hash <> old.content_hash
        or new.document <> old.document
        or new.created_at <> old.created_at then
        raise exception 'protocol_version % content is immutable', old.id;
    end if;
    return new;
end;
$$;

create trigger protocol_version_frozen
    before update or delete on protocol_version
    for each row execute function protocol_version_is_frozen();

-- ---------------------------------------------------------------------------------------------
-- screening

-- Screening: stages per scope, the items and assignments reviewers screen, their
-- decisions, the AI readings, conflicts, and adjudications. Decisions, undos, and adjudications are
-- append-only; an undo is a new row, never a changed decision.

-- Mode S (targeted searches, working documentation) has one screener per record from a pool, a
-- second opinion on request, no auditor, and the AI's reading optionally shown before the
-- decision (reveal `before`).
create table screening_stage (
    id uuid primary key,
    project_id uuid not null references project (id),
    protocol_version_id uuid not null references protocol_version (id),
    protocol_hash char(64) not null,
    topic_id text not null,
    scope_id text not null,
    stage text not null check (stage in ('title_abstract', 'full_text')),
    mode text not null check (mode in ('M1', 'M2', 'M3', 'S')),
    reveal text not null check (reveal in ('never', 'on_disagreement', 'always', 'before')),
    judge_config_id text not null references judge_config (id),
    seed text not null,
    screeners uuid[] not null,
    auditor_id uuid references actor (id),
    adjudicators uuid[] not null,
    opened_event_id uuid not null,
    opened_at timestamptz not null,
    unique (project_id, topic_id, scope_id, stage),
    constraint screening_stage_before_check check (reveal <> 'before' or mode = 'S'),
    constraint screening_stage_auditor_check check ((auditor_id is null) = (mode = 'S'))
);

-- `kind` and `audit` say which items are sentinels or audit samples; `rescreen` marks a record
-- whose AI-only disagreement sent it to the auditor for a blind extra screen. Reviewer reads never
-- select them (see screening_reviewer_queue below).
--
-- A withdrawn item was taken out of screening because the full text shown was not the record's
-- report (a reviewer's "wrong document"). Its assignments and decisions stay on record, but no
-- queue, count, outcome, or report includes it. A withdrawal is not an exclusion.
create table screening_item (
    stage_id uuid not null references screening_stage (id),
    item_ref text not null,
    kind text not null check (kind in ('record', 'sentinel')),
    expected_outcome text check (expected_outcome in ('include', 'exclude')),
    audit boolean not null default false,
    rescreen boolean not null default false,
    title text not null,
    text text not null,
    facts jsonb not null,
    withdrawn_reason text check (withdrawn_reason in ('wrong_document')),
    withdrawn_event_id uuid,
    primary key (stage_id, item_ref),
    check ((kind = 'sentinel') = (expected_outcome is not null)),
    check (not (kind = 'sentinel' and (audit or rescreen))),
    check ((withdrawn_reason is null) = (withdrawn_event_id is null))
);

-- A second opinion is an assignment of another screener of the pool on a record whose screener
-- asked for one. It is active while `consults_decision_id` names that decision; an undo of the
-- decision before anyone answered clears it, and asking again sets it.
create table screening_assignment (
    id uuid primary key,
    stage_id uuid not null,
    item_ref text not null,
    reviewer_id uuid not null references actor (id),
    role text not null check (role in ('screener', 'auditor', 'second_opinion')),
    sort_key char(64) not null,
    state text not null default 'pending'
        check (state in ('pending', 'awaiting_final', 'decided')),
    revealed boolean not null default false,
    revision integer not null default 0,
    etag text not null,
    provisional_decision_id uuid,
    final_decision_id uuid,
    consults_decision_id uuid,
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref),
    unique (stage_id, item_ref, reviewer_id)
);

create index screening_assignment_reviewer on screening_assignment (reviewer_id, stage_id);

-- A bulk decision in a working topic: one batch, one decision per record. A spreadsheet batch
-- holds the decisions brought back from a working export a reviewer edited, with the file's hash
-- and the export it came from: one batch per import, one decision per changed row.
create table screening_batch (
    id uuid primary key,
    stage_id uuid not null references screening_stage (id),
    reviewer_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    action text not null check (action in ('include', 'exclude', 'accept_ai', 'spreadsheet')),
    reason_criterion_id text,
    filter text,
    decisions integer not null,
    recorded_at timestamptz not null,
    file_sha256 char(64),
    file_name text,
    export_id uuid,
    unchanged integer,
    refused integer,
    check ((action = 'exclude') = (reason_criterion_id is not null)),
    constraint screening_batch_spreadsheet_check check (
        (action = 'spreadsheet') = (file_sha256 is not null and export_id is not null
            and unchanged is not null and refused is not null)
    )
);

-- `second_opinion`: the screener asked for one with this decision. `ai_shown_before`: the
-- reviewer had the AI's reading on screen when deciding (reveal `before`). `origin` says where a
-- decision came from: a key on one record (`keyboard`), a bulk decision from the table (`bulk`),
-- or a spreadsheet import (`spreadsheet`), the last two with their batch. `ai_proposal` is the
-- AI's proposal a bulk "accept the AI's proposals" took over. Kittum cannot know when a row of a
-- spreadsheet was decided, or how long it was looked at, so a spreadsheet decision has no timing.
create table screening_decision (
    id uuid primary key,
    assignment_id uuid not null references screening_assignment (id),
    command_id uuid not null unique,
    event_id uuid not null,
    phase text not null check (phase in ('provisional', 'final')),
    outcome text not null
        check (outcome in ('include', 'exclude', 'unclear', 'insufficient_information')),
    reason_criterion_id text,
    criteria jsonb not null,
    evidence_anchor_ids uuid[] not null,
    comment text,
    revision integer not null,
    etag text not null,
    reveal jsonb,
    shown_at timestamptz,
    first_key_at timestamptz,
    decided_at timestamptz,
    reveal_shown_at timestamptz,
    recorded_at timestamptz not null,
    second_opinion boolean not null default false,
    ai_shown_before boolean not null default false,
    origin text not null default 'keyboard'
        constraint screening_decision_origin_check
            check (origin in ('keyboard', 'bulk', 'spreadsheet')),
    batch_id uuid references screening_batch (id),
    ai_proposal jsonb,
    check (outcome <> 'exclude' or reason_criterion_id is not null),
    constraint screening_decision_batch_check check ((origin = 'keyboard') = (batch_id is null)),
    constraint screening_decision_timing_check
        check (origin = 'spreadsheet' or (shown_at is not null and decided_at is not null))
);

alter table screening_assignment
    add foreign key (provisional_decision_id) references screening_decision (id),
    add foreign key (final_decision_id) references screening_decision (id),
    add foreign key (consults_decision_id) references screening_decision (id);

create table screening_undo (
    id uuid primary key,
    assignment_id uuid not null references screening_assignment (id),
    command_id uuid not null unique,
    event_id uuid not null,
    undone_decision_ids uuid[] not null,
    revision integer not null,
    etag text not null,
    recorded_at timestamptz not null
);

-- The AI reading a stage shows now, with the protocol version it was made under; a re-judge
-- replaces it. `screening_ai_reading_log` keeps every reading, the replaced ones included, for
-- reports as of any event and the AI disclosure.
create table screening_ai_reading (
    stage_id uuid not null,
    item_ref text not null,
    judge_config_id text not null,
    excludes boolean not null,
    rank integer not null,
    criteria jsonb not null,
    flags text[] not null,
    raw_output_hash char(64) not null,
    event_id uuid not null,
    recorded_at timestamptz not null,
    protocol_hash char(64) not null,
    primary key (stage_id, item_ref),
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref)
);

create table screening_ai_reading_log (
    stage_id uuid not null,
    item_ref text not null,
    judge_config_id text not null,
    protocol_hash char(64) not null,
    excludes boolean not null,
    rank integer not null,
    criteria jsonb not null,
    flags text[] not null,
    raw_output_hash char(64) not null,
    event_id uuid not null,
    recorded_at timestamptz not null,
    primary key (stage_id, item_ref, event_id),
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref)
);

-- Conflicts are human disagreements only: an AI-only disagreement becomes a blind extra screen in
-- the audit stream instead. IDs are random, and `etag` is the opaque concurrency token clients see.
create table screening_conflict (
    id uuid primary key,
    stage_id uuid not null,
    item_ref text not null,
    kinds text[] not null,
    state text not null check (state in ('open', 'resolved', 'withdrawn')),
    revision integer not null default 0,
    etag text not null,
    opened_event_id uuid not null,
    opened_at timestamptz not null,
    closed_event_id uuid,
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref)
);

create unique index screening_conflict_open on screening_conflict (stage_id, item_ref)
    where state = 'open';

create table screening_adjudication (
    id uuid primary key,
    conflict_id uuid not null references screening_conflict (id),
    adjudicator_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    adopted text,
    outcome text not null
        check (outcome in ('include', 'exclude', 'unclear', 'insufficient_information')),
    reason_criterion_id text,
    criteria jsonb not null,
    evidence_anchor_ids uuid[] not null,
    comment text,
    shown_at timestamptz,
    decided_at timestamptz,
    recorded_at timestamptz not null,
    check (outcome <> 'exclude' or reason_criterion_id is not null)
);

-- A stage re-judged under another published protocol version: the stage reads with it from then
-- on. Only the AI's instructions may differ (the screening service checks), so human decisions
-- keep their meaning.
create table screening_rejudge (
    id uuid primary key,
    stage_id uuid not null references screening_stage (id),
    from_protocol_version_id uuid not null references protocol_version (id),
    to_protocol_version_id uuid not null references protocol_version (id),
    from_protocol_hash char(64) not null,
    to_protocol_hash char(64) not null,
    actor_id uuid not null references actor (id),
    event_id uuid not null,
    recorded_at timestamptz not null
);

-- A test bench run: the judge on a stage's records with a draft protocol's instructions. Its
-- verdicts are the judge's own rows; they never become the stage's readings.
create table screening_bench (
    id uuid primary key,
    project_id uuid not null references project (id),
    stage_id uuid not null references screening_stage (id),
    topic_id text not null,
    protocol jsonb not null,
    protocol_hash char(64) not null,
    item_refs text[] not null,
    known_refs text[] not null,
    requested_by uuid not null references actor (id),
    requested_event_id uuid not null,
    recorded_at timestamptz not null,
    verdicts jsonb,
    finished_event_id uuid,
    finished_at timestamptz
);

create index screening_bench_stage on screening_bench (stage_id, recorded_at);

create function screening_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception '% rows are append-only (% on %)', tg_table_name, tg_op, old.id;
end;
$$;

create trigger screening_decision_append_only
    before update or delete on screening_decision
    for each row execute function screening_is_append_only();

create trigger screening_undo_append_only
    before update or delete on screening_undo
    for each row execute function screening_is_append_only();

create trigger screening_adjudication_append_only
    before update or delete on screening_adjudication
    for each row execute function screening_is_append_only();

-- What a reviewer may read: their own assignments in one stage, in queue order. No sentinel or
-- audit flag, no AI output the stage does not reveal, nothing of another reviewer, no withdrawn
-- item. An item enters the queue once the AI has read it, except in an S stage, which does not
-- wait for the judge. The reveal of the reviewer's own provisional decision comes back only while
-- that decision awaits its final phase. A stage that reveals `before` returns the AI's reading as
-- `ai_before` once there is one. A second opinion comes first, while it is active, with the
-- decision it is asked on as `consult`. `position` is an ordinal: M3 orders by the AI's rank, the
-- other modes by a seeded key.
create function screening_reviewer_queue(p_stage_id uuid, p_reviewer uuid) returns table (
    assignment_id uuid,
    stage text,
    scope_id text,
    title text,
    text text,
    facts jsonb,
    etag text,
    state text,
    own_reveal jsonb,
    ai_before jsonb,
    consult jsonb,
    "position" bigint
)
language sql stable as $$
    select a.id, s.stage, s.scope_id, i.title, i.text, i.facts, a.etag, a.state,
        case when a.state = 'awaiting_final' then d.reveal end,
        case when s.reveal = 'before' and r.item_ref is not null then
            jsonb_build_object('excludes', r.excludes, 'criteria', r.criteria)
        end,
        case when c.id is not null then
            jsonb_build_object(
                'outcome', c.outcome,
                'reason_criterion_id', c.reason_criterion_id,
                'comment', c.comment,
                'criteria', c.criteria
            )
        end,
        row_number() over (
            order by a.role = 'second_opinion' desc,
                case when s.mode = 'M3' then r.rank else 0 end, a.sort_key, a.id
        )
    from screening_assignment a
    join screening_stage s on s.id = a.stage_id
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    left join screening_ai_reading r on r.stage_id = a.stage_id and r.item_ref = a.item_ref
    left join screening_decision d on d.id = a.provisional_decision_id
    left join screening_decision c on c.id = a.consults_decision_id
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
        and i.withdrawn_event_id is null
        and (r.item_ref is not null or s.mode = 'S')
        and (a.role <> 'second_opinion' or a.consults_decision_id is not null)
$$;

-- A reviewer's own progress: every assignment counts, ready or not, hidden items included, so
-- the counts cannot single out a sentinel or an audit sample. Withdrawn items and inactive second
-- opinions do not count.
create function screening_reviewer_progress(p_stage_id uuid, p_reviewer uuid)
returns table (assigned bigint, decided bigint)
language sql stable as $$
    select count(*), count(*) filter (where a.state = 'decided')
    from screening_assignment a
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
        and i.withdrawn_event_id is null
        and (a.role <> 'second_opinion' or a.consults_decision_id is not null)
$$;

-- The reviewer's table: every one of their own assignments the queue holds or held, decided ones
-- included, with their own decision and the AI reading their own provisional decision revealed.
-- It lists what screening_reviewer_queue lists, in the same order (`position`): in S the records
-- the judge has not read yet, and a second opinion while it is active, with the decision it is
-- asked on (`consult`); a stage that reveals `before` returns the AI's reading as `ai_before`.
-- Like the queue it cannot return another reviewer's rows (but a consult's decision), an AI
-- reading the reviewer may not see yet, or sentinel and audit flags. The item reference is left
-- out too, because a full-text sentinel's reference names it as one.
create function screening_reviewer_table(p_stage_id uuid, p_reviewer uuid) returns table (
    assignment_id uuid,
    title text,
    text text,
    facts jsonb,
    etag text,
    state text,
    "position" bigint,
    outcome text,
    reason_criterion_id text,
    comment text,
    decided_at timestamptz,
    asked_second_opinion boolean,
    own_reveal jsonb,
    ai_before jsonb,
    consult jsonb
)
language sql stable as $$
    select a.id, i.title, i.text, i.facts, a.etag, a.state,
        row_number() over (
            order by a.role = 'second_opinion' desc,
                case when s.mode = 'M3' then r.rank else 0 end, a.sort_key, a.id
        ),
        d.outcome, d.reason_criterion_id, d.comment, d.decided_at,
        coalesce(d.second_opinion, false),
        -- What the reviewer was shown, kept when they take the decision back and decide again.
        case when a.revealed then (
            select r2.reveal from screening_decision r2
            where r2.assignment_id = a.id and r2.reveal is not null
            order by r2.recorded_at desc, r2.id desc limit 1
        ) end,
        case when s.reveal = 'before' and r.item_ref is not null then
            jsonb_build_object('excludes', r.excludes, 'criteria', r.criteria)
        end,
        case when c.id is not null then
            jsonb_build_object(
                'outcome', c.outcome,
                'reason_criterion_id', c.reason_criterion_id,
                'comment', c.comment,
                'criteria', c.criteria
            )
        end
    from screening_assignment a
    join screening_stage s on s.id = a.stage_id
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    left join screening_ai_reading r on r.stage_id = a.stage_id and r.item_ref = a.item_ref
    left join screening_decision d on d.id = coalesce(a.final_decision_id, a.provisional_decision_id)
    left join screening_decision c on c.id = a.consults_decision_id
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
        and i.withdrawn_event_id is null
        and (r.item_ref is not null or s.mode = 'S')
        and (a.role <> 'second_opinion' or a.consults_decision_id is not null)
$$;

-- ---------------------------------------------------------------------------------------------
-- report

-- A rendered report target: its files are in the artifact store by hash, and `index_hash` is the
-- canonical JSON of the whole artifact. Rows are append-only; a new render is a new row.
create table report_artifact (
    id uuid primary key,
    project_id uuid not null references project (id),
    target text not null,
    scope_id text,
    cutoff_seq bigint not null check (cutoff_seq >= 0),
    identity_revision_id uuid references identity_revision (id),
    index_hash char(64) not null,
    files jsonb not null,
    actor_id uuid not null references actor (id),
    event_id uuid not null references audit_event (event_id),
    rendered_at timestamptz not null
);

create index report_artifact_project on report_artifact (project_id, rendered_at);
create index report_artifact_files on report_artifact using gin (files jsonb_path_ops);

create function report_artifact_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'report_artifact rows are append-only (% on %)', tg_op, old.id;
end;
$$;

create trigger report_artifact_append_only
    before update or delete on report_artifact
    for each row execute function report_artifact_is_append_only();

-- ---------------------------------------------------------------------------------------------
-- extraction

-- Extraction runs, the fields they review, both paths' proposals, and human acceptances
-- (DESIGN.md, "Extraction"). Anchors live in the documents module's `anchor` table. A run's
-- proposals never change; an acceptance is a new row with its own ledger event. Each row keeps
-- the ledger sequence number of the event that wrote it, so a bundle can export the state as of
-- a cutoff.

create table extraction_run (
    id uuid primary key,
    project_id uuid not null references project (id),
    command_id uuid not null unique,
    layer_id uuid not null references layer (id),
    document_id uuid not null references document (id),
    study_id uuid not null,
    report_id uuid not null,
    report jsonb not null,
    schema jsonb not null,
    schema_hash char(64) not null,
    agent jsonb,
    status text not null check (status in ('queued', 'succeeded', 'failed')),
    requested_by uuid not null,
    requested_seq bigint not null,
    publish_command_id uuid not null unique,
    janus_version text,
    model_identity text,
    transcript_hash char(64),
    output_hash char(64),
    recorded_seq bigint,
    created_at timestamptz not null,
    completed_at timestamptz,
    check ((status = 'succeeded') = (recorded_seq is not null))
);

create index extraction_run_by_project on extraction_run (project_id, created_at);

create table extraction_field (
    id uuid primary key,
    run_id uuid not null references extraction_run (id),
    project_id uuid not null references project (id),
    ordinal integer not null,
    key jsonb not null,
    key_hash char(64) not null,
    spec jsonb not null,
    kind text not null check (kind in ('count', 'decimal', 'percentage', 'ratio', 'p_value', 'text')),
    labels jsonb not null,
    agreement text not null
        check (agreement in ('agree', 'disagree', 'janus_only', 'llm_only', 'neither')),
    gate text not null check (gate in ('human_verify', 'group_accept', 'human_review')),
    second_extractor boolean not null,
    recheck boolean not null,
    recorded_seq bigint not null,
    unique (run_id, key_hash),
    unique (run_id, ordinal)
);

create table extraction_proposal (
    id uuid primary key,
    field_id uuid not null references extraction_field (id),
    run_id uuid not null references extraction_run (id),
    path text not null check (path in ('janus', 'llm')),
    value text,
    unit text,
    status text not null check (status in ('extracted', 'missing', 'ambiguous', 'invalid', 'error')),
    message text,
    quote text,
    anchor_ids uuid[] not null,
    validators jsonb not null,
    recorded_seq bigint not null,
    unique (field_id, path),
    check (status <> 'extracted' or (value is not null and cardinality(anchor_ids) = 1))
);

create table extraction_acceptance (
    id uuid primary key,
    field_id uuid not null unique references extraction_field (id),
    run_id uuid not null references extraction_run (id),
    project_id uuid not null references project (id),
    key_hash char(64) not null,
    path text not null check (path in ('janus', 'llm')),
    edited boolean not null,
    grouped boolean not null,
    value text,
    unit text,
    anchor_id uuid not null references anchor (id),
    reviewer_id uuid not null,
    source_viewed_ms integer not null check (source_viewed_ms >= 0),
    recheck boolean not null,
    accepted_at timestamptz not null,
    event_seq bigint not null
);

create index extraction_acceptance_by_key on extraction_acceptance (project_id, key_hash, event_seq);

-- ---------------------------------------------------------------------------------------------
-- assist

-- The protocol assistant's revisions (kittum.assist). Every draft a model writes, every
-- suggestion it makes, every change a lead accepts, edits, or restores is a new row; none is ever
-- changed or deleted, so stepping back is reading an older row. What a model wrote is read from
-- this history by value (kittum.assist), never stored beside a revision. Every document compiles: an
-- answer that does not is never stored as a revision (its attempts are in the ledger event).

create table assist_revision (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    seq integer not null check (seq > 0),
    parent_id uuid references assist_revision (id),
    kind text not null check (kind in ('draft', 'suggestion', 'accepted', 'edit', 'restore')),
    document json not null,  -- json, not jsonb: the document exactly as compiled, keys in order
    content_hash char(64) not null,
    actor_id uuid not null references actor (id),
    model jsonb,
    suggestion jsonb,
    accepted jsonb,
    note text,
    created_at timestamptz not null default now(),
    unique (project_id, seq),
    check ((kind in ('draft', 'suggestion')) = (model is not null)),
    check ((kind = 'suggestion') = (suggestion is not null)),
    check ((kind = 'accepted') = (accepted is not null))
);

create index assist_revision_by_hash on assist_revision (project_id, content_hash);

create function assist_revision_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'assist_revision % is append-only', old.id;
end;
$$;

create trigger assist_revision_append_only
    before update or delete on assist_revision
    for each row execute function assist_revision_is_append_only();

-- ---------------------------------------------------------------------------------------------
-- appraisal

-- Risk-of-bias appraisals (DESIGN.md, "Appraisal"). One appraisal per report, instrument, and
-- result. Answers and overrides are appended, never edited: the newest row per question, or per
-- domain or the overall judgement, is the current one. Each row keeps the ledger sequence number
-- of the event that wrote it, so a report can show the appraisals as of a cutoff. `command_id`
-- makes a retried request return the first result.

create table appraisal (
    id uuid primary key,
    project_id uuid not null references project (id),
    report_id uuid not null,
    study_id uuid not null,
    document_id uuid not null references document (id),
    study_label text not null,
    report_title text not null,
    instrument text not null,
    result text not null check (length(btrim(result)) > 0),
    created_by uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    event_seq bigint not null,
    created_at timestamptz not null,
    unique (project_id, report_id, instrument, result)
);

create index appraisal_by_report on appraisal (project_id, report_id);

create table appraisal_answer (
    id uuid primary key,
    appraisal_id uuid not null references appraisal (id),
    project_id uuid not null references project (id),
    question_id text not null,
    answer text not null
        check (answer in ('yes', 'probably_yes', 'probably_no', 'no', 'no_information')),
    anchor_ids uuid[] not null,
    actor_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    event_seq bigint not null,
    recorded_at timestamptz not null
);

create index appraisal_answer_by_appraisal on appraisal_answer (appraisal_id, question_id, event_seq);

-- `target` is a domain ID or 'overall'. An override needs a reason; a null judgement takes the
-- override back.
create table appraisal_override (
    id uuid primary key,
    appraisal_id uuid not null references appraisal (id),
    project_id uuid not null references project (id),
    target text not null,
    judgement text check (judgement in ('low', 'some_concerns', 'high')),
    reason text,
    actor_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    event_seq bigint not null,
    recorded_at timestamptz not null,
    check (judgement is null or length(btrim(coalesce(reason, ''))) > 0)
);

create index appraisal_override_by_appraisal on appraisal_override (appraisal_id, target, event_seq);

-- ---------------------------------------------------------------------------------------------
-- app

-- Who works on a project and in which role. A person can hold several roles; screening checks
-- the rules between them (an auditor never screens the same stage) when a stage opens.
create table project_member (
    project_id uuid not null references project (id),
    actor_id uuid not null references actor (id),
    role text not null check (role in ('lead', 'reviewer', 'auditor', 'adjudicator')),
    display_name text not null,
    event_id uuid not null,
    created_at timestamptz not null default now(),
    primary key (project_id, actor_id, role)
);

-- The full text attached to a record (a corpus report), by its screening item reference. One per
-- record; attaching another PDF later is a new document and needs a new full-text stage. A
-- sentinel's full text (a report with a known outcome, seeded into full-text queues) has an
-- item reference of its own, a title, and the outcome it should get.
create table full_text (
    project_id uuid not null references project (id),
    item_ref text not null,
    document_id uuid not null references document (id),
    layer_id uuid not null references layer (id),
    licence text,
    sentinel_title text,
    sentinel_outcome text check (sentinel_outcome in ('include', 'exclude')),
    check ((sentinel_title is null) = (sentinel_outcome is null)),
    event_id uuid not null,
    created_at timestamptz not null default now(),
    primary key (project_id, item_ref)
);

-- A full text taken off its record because it is not the record's report (a reviewer's "wrong
-- document"). The record has no full text again and can get another one. The detached document
-- stays stored, and the ledger event names it. `command_id` makes a retried request a no-op.
create table full_text_detachment (
    id uuid primary key,
    project_id uuid not null references project (id),
    item_ref text not null,
    document_id uuid not null references document (id),
    reason text not null check (reason in ('wrong_document')),
    note text,
    actor_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    recorded_at timestamptz not null
);

create index full_text_detachment_item on full_text_detachment (project_id, item_ref);

-- Enrichment runs over a corpus revision, and open-access retrieval runs over the reports sought
-- for retrieval. Each run is one job; these rows hold what it did, record by record and report by
-- report, so a retried job resumes where it stopped and the workbench shows its progress. The
-- enriched fields themselves are `record_enrichment` rows; the files are `acquisition` rows.
--
-- An enrichment run can cover some records only (`occurrence_ids`, null for every record of the
-- revision), and a retry run names the reports whose open-access retrieval follows it once it is
-- done (`retrieve_after`).
create table enrichment_run (
    id uuid primary key,
    project_id uuid not null references project (id),
    identity_revision_id uuid not null,
    trigger text not null check (trigger in ('corpus_built', 'requested', 'retry')),
    requested_by uuid not null references actor (id),
    requested_at timestamptz not null,
    records integer not null,
    job_id uuid not null references job (id),
    event_id uuid not null,
    finished_at timestamptz,
    occurrence_ids jsonb,
    retrieve_after jsonb
);

create index enrichment_run_by_project on enrichment_run (project_id, requested_at);

-- One row per source occurrence the run enriched. `asked` and `answered` list sources; `failed`
-- holds `{source, reason}` for each source that failed without answering for this record.
create table enrichment_run_record (
    run_id uuid not null references enrichment_run (id),
    occurrence_id uuid not null,
    asked jsonb not null,
    answered jsonb not null,
    failed jsonb not null,
    retracted boolean not null,
    event_id uuid not null,
    primary key (run_id, occurrence_id)
);

-- A retrieval run says what asked for it and which lookup sources its worker left out, with the
-- reason (`skipped`, `[{source, reason}]`).
create table retrieval_run (
    id uuid primary key,
    project_id uuid not null references project (id),
    requested_by uuid not null references actor (id),
    requested_at timestamptz not null,
    job_id uuid not null references job (id),
    event_id uuid not null,
    finished_at timestamptz,
    trigger text not null default 'requested' check (trigger in ('requested', 'retry')),
    skipped jsonb not null default '[]'::jsonb
);

create index retrieval_run_by_project on retrieval_run (project_id, requested_at);

-- The reports a run walks the waterfall for, fixed when it is requested. Each report's state is
-- resumable: `locations` (`documents.FullTextLocation`s) are fixed when the job first takes the
-- report, every attempt is appended to `attempts` in a transaction of its own with its ledger
-- event, `acquisition_id` is set by the attempt that got a PDF, and `outcome` once the report is
-- finished (after the PDF is attached, in another transaction). A retry's report starts with the
-- attempts earlier runs ended for good (`carried`): a lookup whose source answered for another
-- work, a PDF that was another document. Its waterfall skips them.
create table retrieval_run_report (
    run_id uuid not null references retrieval_run (id),
    report_id uuid not null,
    ordinal integer not null,
    outcome text check (outcome in ('acquired', 'not_acquired', 'no_location', 'had_full_text')),
    locations jsonb,
    attempts jsonb not null default '[]'::jsonb,
    acquisition_id uuid references acquisition (id),
    event_id uuid,
    finished_at timestamptz,
    carried jsonb not null default '[]'::jsonb,
    primary key (run_id, report_id)
);

-- One row per PDF of a bulk upload. A PDF that matched one report is `attached` at once; one that
-- matched none, or more than one, is `held` for the lead with the reports it could be
-- (`candidates`), until the lead assigns it (`assigned`) or discards it (`discarded`).
create table retrieval_upload (
    id uuid primary key,
    project_id uuid not null references project (id),
    document_id uuid not null references document (id),
    file_name text not null,
    licence text,
    uploaded_by uuid not null references actor (id),
    uploaded_at timestamptz not null,
    status text not null check (status in ('attached', 'held', 'assigned', 'discarded')),
    matched_by text check (matched_by in ('file_name', 'content', 'lead')),
    reason text,
    candidates jsonb not null default '[]'::jsonb,
    item_ref text,
    event_id uuid not null
);

create index retrieval_upload_by_project on retrieval_upload (project_id, status, uploaded_at);

-- migrate:down

drop table retrieval_upload;
drop table retrieval_run_report;
drop table retrieval_run;
drop table enrichment_run_record;
drop table enrichment_run;
drop table full_text_detachment;
drop table full_text;
drop table project_member;
drop table appraisal_override;
drop table appraisal_answer;
drop table appraisal;
drop table assist_revision;
drop function assist_revision_is_append_only;
drop table extraction_acceptance;
drop table extraction_proposal;
drop table extraction_field;
drop table extraction_run;
drop table report_artifact;
drop function report_artifact_is_append_only;
drop function screening_reviewer_table;
drop function screening_reviewer_progress;
drop function screening_reviewer_queue;
drop table screening_bench;
drop table screening_rejudge;
drop table screening_adjudication;
drop table screening_conflict;
drop table screening_ai_reading_log;
drop table screening_ai_reading;
drop table screening_undo;
alter table screening_assignment
    drop constraint screening_assignment_provisional_decision_id_fkey,
    drop constraint screening_assignment_final_decision_id_fkey,
    drop constraint screening_assignment_consults_decision_id_fkey;
drop table screening_decision;
drop table screening_batch;
drop table screening_assignment;
drop table screening_item;
drop table screening_stage;
drop function screening_is_append_only;
drop table protocol_version;
drop function protocol_version_is_frozen;
drop table verdict;
drop function verdict_is_immutable;
drop table judge_config;
drop table record_enrichment;
drop table page_render;
drop table anchor_mapping;
drop table anchor;
drop table layer;
drop function documents_immutable;
drop table acquisition;
drop table document;
drop table known_pool_entry;
drop table known_pool_file;
drop table proposal;
drop table identity_link;
drop table identity_revision;
drop function identity_link_revision_is_open;
drop function identity_revision_is_finalized;
drop function identity_revision_only_finalizes;
drop function identity_is_append_only;
drop table corpus_revision;
drop table study;
drop table report;
drop table source_occurrence;
drop table search_execution;
drop table gateway_cooldown;
drop table gateway_budget;
drop table gateway_cache;
drop table gateway_attempt;
drop table job;
drop table command_receipt;
drop table checkpoint;
drop table audit_event;
drop function audit_event_is_append_only;
drop table actor;
drop table project;
