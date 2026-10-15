-- 001_init.sql — начальная схема базы «Навигатора по олимпиадам».
--
-- Принципы:
--   * Перечень олимпиад хранится по учебным годам: уровень олимпиады меняется
--     от года к году, а диплом действует 4 года с уровнем года получения.
--   * Льготы ссылаются на олимпиаду и НАЗВАНИЕ профиля, а не на строку перечня,
--     поэтому перечень года можно перезагружать целиком, не ломая льготы.
--   * У каждой льготы есть год приёма, статус проверки и ссылка на источник.

-- ---------------------------------------------------------------------------
-- Олимпиады и перечень
-- ---------------------------------------------------------------------------

CREATE TABLE olympiad (
    id          bigserial PRIMARY KEY,
    key         text NOT NULL UNIQUE,          -- нормализованное название, стабильное между годами
    name        text NOT NULL,                 -- официальное название из самого свежего перечня
    name_year   text NOT NULL,                 -- учебный год, из которого взято name
    is_vsosh    boolean NOT NULL DEFAULT false,-- Всероссийская олимпиада школьников (вне перечня РСОШ)
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Строка перечня: профиль олимпиады в конкретном учебном году.
CREATE TABLE olympiad_profile (
    id             bigserial PRIMARY KEY,
    olympiad_id    bigint NOT NULL REFERENCES olympiad(id) ON DELETE CASCADE,
    academic_year  text   NOT NULL CHECK (academic_year ~ '^\d{4}/\d{2}$'),  -- '2026/27'
    source         text   NOT NULL DEFAULT 'rsr' CHECK (source IN ('rsr', 'vsosh', 'manual')),
    list_number    integer,                    -- № в перечне (NULL для ВсОШ)
    name_in_list   text   NOT NULL,            -- название так, как оно записано в перечне этого года
    profile        text   NOT NULL,            -- профиль олимпиады: «математика», «инженерное дело»
    subjects_raw   text   NOT NULL,            -- колонка «Предметы» как есть
    subjects       text[] NOT NULL DEFAULT '{}', -- она же, разобранная на отдельные предметы/области
    level          smallint CHECK (level BETWEEN 1 AND 3), -- NULL для ВсОШ
    website        text,                       -- сайт олимпиады (может отличаться по профилям)
    source_url     text,                       -- откуда загружено
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (olympiad_id, academic_year, profile)
);
CREATE INDEX olympiad_profile_year_idx ON olympiad_profile (academic_year);
CREATE INDEX olympiad_profile_subjects_idx ON olympiad_profile USING gin (subjects);

-- Этапы олимпиады: регистрация, отборочный, заключительный, результаты.
CREATE TABLE stage (
    id             bigserial PRIMARY KEY,
    olympiad_id    bigint NOT NULL REFERENCES olympiad(id) ON DELETE CASCADE,
    academic_year  text   NOT NULL CHECK (academic_year ~ '^\d{4}/\d{2}$'),
    profile        text,                       -- NULL = этап общий для всех профилей
    kind           text   NOT NULL CHECK (kind IN ('registration', 'qualifying', 'final', 'results', 'other')),
    title          text   NOT NULL,
    starts_at      date,
    ends_at        date,
    grades         smallint[],                 -- для каких классов, если указано
    source_url     text,
    status         text   NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'cancelled')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
);
CREATE INDEX stage_starts_idx ON stage (starts_at);
CREATE INDEX stage_olympiad_idx ON stage (olympiad_id, academic_year);

-- ---------------------------------------------------------------------------
-- Вузы, направления, льготы
-- ---------------------------------------------------------------------------

CREATE TABLE university (
    id             bigserial PRIMARY KEY,
    short_name     text NOT NULL UNIQUE,       -- «МГУ», «ВШЭ», «МФТИ»
    full_name      text NOT NULL,
    city           text,
    website        text,
    admission_url  text,                       -- страница приёма, которую отслеживаем
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Направление подготовки (программа) в вузе на конкретный год приёма.
CREATE TABLE program (
    id               bigserial PRIMARY KEY,
    university_id    bigint  NOT NULL REFERENCES university(id) ON DELETE CASCADE,
    admission_year   integer NOT NULL CHECK (admission_year BETWEEN 2020 AND 2100), -- 2027 = приём летом 2027
    code             text    NOT NULL CHECK (code ~ '^\d{2}\.\d{2}\.\d{2}$'),        -- '01.03.02'
    name             text    NOT NULL,         -- «Прикладная математика и информатика»
    faculty          text,
    -- Вступительные испытания. Каждый элемент — одно испытание, внутри — допустимые предметы на выбор:
    -- [{"subjects": ["математика"], "min_score": 60}, {"subjects": ["информатика", "физика"], "min_score": 55}]
    ege              jsonb   NOT NULL DEFAULT '[]',
    source_url       text,
    UNIQUE NULLS NOT DISTINCT (university_id, admission_year, code, name, faculty)
);
CREATE INDEX program_code_idx ON program (code);

-- Льгота: что даёт диплом олимпиады при поступлении в вуз.
CREATE TABLE benefit (
    id                 bigserial PRIMARY KEY,
    university_id      bigint  NOT NULL REFERENCES university(id) ON DELETE CASCADE,
    program_id         bigint  REFERENCES program(id) ON DELETE CASCADE, -- NULL = все направления из applies_to
    applies_to         text,                   -- как в документе вуза, если program_id не определён
    admission_year     integer NOT NULL CHECK (admission_year BETWEEN 2020 AND 2100),
    olympiad_id        bigint  NOT NULL REFERENCES olympiad(id) ON DELETE CASCADE,
    profile            text,                   -- NULL = любой профиль олимпиады
    levels             smallint[] NOT NULL DEFAULT '{1,2,3}',
    diploma            text    NOT NULL CHECK (diploma IN ('winner', 'prize', 'any')), -- победитель / призёр / оба
    benefit_type       text    NOT NULL CHECK (benefit_type IN ('bvi', 'score100')),
    grades             smallint[] NOT NULL DEFAULT '{10,11}', -- в каком классе получен диплом
    confirm_subject    text,                   -- ЕГЭ, которым подтверждается льгота
    confirm_min_score  smallint CHECK (confirm_min_score BETWEEN 0 AND 100),
    -- draft      — извлечено автоматически, не проверено
    -- prev_year  — данные прошлого года приёма, показываем с плашкой
    -- confirmed  — проверено человеком по документу этого года
    status             text    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'prev_year', 'confirmed', 'archived')),
    source_url         text    NOT NULL,       -- первоисточник обязателен
    checked_at         date,
    notes              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (levels <@ '{1,2,3}'::smallint[]),
    CHECK (program_id IS NOT NULL OR applies_to IS NOT NULL)
);
CREATE INDEX benefit_olympiad_idx ON benefit (olympiad_id, admission_year);
CREATE INDEX benefit_university_idx ON benefit (university_id, admission_year);
CREATE INDEX benefit_program_idx ON benefit (program_id);

-- ---------------------------------------------------------------------------
-- Пользователи бота и подписки
-- ---------------------------------------------------------------------------

-- Храним минимум персональных данных: только Telegram ID и настройки.
CREATE TABLE bot_user (
    id           bigserial PRIMARY KEY,
    telegram_id  bigint NOT NULL UNIQUE,
    grade        smallint CHECK (grade BETWEEN 1 AND 11),
    is_active    boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subscription (
    id             bigserial PRIMARY KEY,
    user_id        bigint NOT NULL REFERENCES bot_user(id) ON DELETE CASCADE,
    olympiad_id    bigint REFERENCES olympiad(id) ON DELETE CASCADE,
    university_id  bigint REFERENCES university(id) ON DELETE CASCADE,
    profile        text,                       -- NULL = все профили олимпиады
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (olympiad_id IS NOT NULL OR university_id IS NOT NULL),
    UNIQUE NULLS NOT DISTINCT (user_id, olympiad_id, university_id, profile)
);

-- Журнал отправленных уведомлений, чтобы не слать одно и то же дважды.
CREATE TABLE notification_log (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES bot_user(id) ON DELETE CASCADE,
    stage_id   bigint REFERENCES stage(id) ON DELETE CASCADE,
    kind       text   NOT NULL,                -- 'stage_t7', 'stage_t1', 'stage_day', 'benefits_published'
    ref        text,                           -- для уведомлений без этапа (например, id вуза)
    sent_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (user_id, stage_id, kind, ref)
);

-- ---------------------------------------------------------------------------
-- Мониторинг источников
-- ---------------------------------------------------------------------------

CREATE TABLE source_page (
    id               bigserial PRIMARY KEY,
    url              text NOT NULL UNIQUE,
    kind             text NOT NULL CHECK (kind IN ('rsr_list', 'olympiad', 'calendar', 'admission', 'channel')),
    olympiad_id      bigint REFERENCES olympiad(id) ON DELETE SET NULL,
    university_id    bigint REFERENCES university(id) ON DELETE SET NULL,
    css_selector     text,                     -- какую часть страницы сравнивать
    check_interval   interval NOT NULL DEFAULT '6 hours',
    content_hash     text,
    last_checked_at  timestamptz,
    last_changed_at  timestamptz,
    last_error       text
);

CREATE TABLE page_change (
    id              bigserial PRIMARY KEY,
    source_page_id  bigint NOT NULL REFERENCES source_page(id) ON DELETE CASCADE,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    diff            text NOT NULL,
    status          text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'processed', 'ignored'))
);
CREATE INDEX page_change_status_idx ON page_change (status);
