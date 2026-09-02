/* ==========================================================================
   Confluence — Control Plane
   Azure SQL Database (serverless, auto-pause 60 min)

   Four tables and three procedures. Together they mean ADF contains no
   hardcoded source list: one parameterised pipeline reads this manifest,
   loops it, and switches on load_type. Adding a source is an INSERT, not
   a pipeline change.

   Run once against an empty database.
   ========================================================================== */

IF SCHEMA_ID('ctl') IS NULL EXEC('CREATE SCHEMA ctl');
GO

/* --------------------------------------------------------------------------
   1. IngestionSource — the manifest. ADF's Lookup activity reads this.
   -------------------------------------------------------------------------- */
IF OBJECT_ID('ctl.IngestionSource') IS NOT NULL DROP TABLE ctl.IngestionSource;
GO
CREATE TABLE ctl.IngestionSource (
    source_id           INT           IDENTITY(1,1) PRIMARY KEY,
    source_name         NVARCHAR(100) NOT NULL UNIQUE,
    source_group        NVARCHAR(50)  NOT NULL,   -- olist | za_platform | merchant_master | api
    platform_code       NVARCHAR(2)   NULL,       -- BR | ZA | NULL for shared reference data
    source_type         NVARCHAR(20)  NOT NULL,   -- file | rest
    load_type           NVARCHAR(20)  NOT NULL,   -- FULL | INCREMENTAL | SNAPSHOT

    -- file sources
    source_folder       NVARCHAR(300) NULL,
    file_pattern        NVARCHAR(200) NULL,       -- glob; ADF Get Metadata expands it
    file_format         NVARCHAR(20)  NULL,       -- csv | json
    column_delimiter    NVARCHAR(5)   NULL,       -- ',' or ';'
    date_format         NVARCHAR(30)  NULL,       -- dd/MM/yyyy where the source is ambiguous
    file_encoding       NVARCHAR(20)  NULL,

    -- rest sources
    rest_base_url       NVARCHAR(300) NULL,
    rest_path_template  NVARCHAR(400) NULL,       -- @{} tokens resolved by ADF
    rest_iterator       NVARCHAR(50)  NULL,       -- what the ForEach loops over

    -- landing
    landing_container   NVARCHAR(100) NOT NULL,
    landing_folder      NVARCHAR(300) NOT NULL,

    -- incremental control
    watermark_column    NVARCHAR(100) NULL,
    watermark_type      NVARCHAR(20)  NULL,       -- datetime | date | filename

    -- quality gate: a load returning fewer rows than this fails the pipeline
    expected_min_rows   INT           NULL,

    load_order          INT           NOT NULL DEFAULT 100,
    is_active           BIT           NOT NULL DEFAULT 1,
    created_at          DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at          DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
CREATE INDEX IX_IngestionSource_Active ON ctl.IngestionSource (is_active, load_order);
GO

/* --------------------------------------------------------------------------
   2. Watermark — where each incremental source got to.

   Kept separate from the manifest on purpose. The manifest is configuration
   and belongs in source control; the watermark is runtime state and does not.
   -------------------------------------------------------------------------- */
IF OBJECT_ID('ctl.Watermark') IS NOT NULL DROP TABLE ctl.Watermark;
GO
CREATE TABLE ctl.Watermark (
    source_id           INT           NOT NULL PRIMARY KEY
        REFERENCES ctl.IngestionSource(source_id),
    watermark_value     NVARCHAR(100) NULL,       -- string form; cast on read
    previous_value      NVARCHAR(100) NULL,       -- one step of history, for rollback
    last_updated        DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    last_run_id         UNIQUEIDENTIFIER NULL
);
GO

/* --------------------------------------------------------------------------
   3. PipelineRun — the audit log. Every activity writes here, pass or fail.

   This is what you screenshot for the portfolio, and what proves the
   pipeline ran rather than merely existed.
   -------------------------------------------------------------------------- */
IF OBJECT_ID('ctl.PipelineRun') IS NOT NULL DROP TABLE ctl.PipelineRun;
GO
CREATE TABLE ctl.PipelineRun (
    run_id              UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
    adf_pipeline_name   NVARCHAR(200) NOT NULL,
    adf_run_id          NVARCHAR(100) NULL,       -- @pipeline().RunId
    source_id           INT           NULL REFERENCES ctl.IngestionSource(source_id),
    load_type           NVARCHAR(20)  NULL,
    window_start        NVARCHAR(100) NULL,       -- watermark floor used
    window_end          NVARCHAR(100) NULL,       -- ceiling frozen BEFORE the copy
    started_at          DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    ended_at            DATETIME2(0)  NULL,
    status              NVARCHAR(20)  NOT NULL DEFAULT 'RUNNING',  -- RUNNING|SUCCEEDED|FAILED
    files_read          INT           NULL,
    rows_read           BIGINT        NULL,
    rows_written        BIGINT        NULL,
    rows_quarantined    BIGINT        NULL,
    error_message       NVARCHAR(MAX) NULL
);
GO
CREATE INDEX IX_PipelineRun_Source ON ctl.PipelineRun (source_id, started_at DESC);
GO

/* --------------------------------------------------------------------------
   4. Quarantine — rows that failed a quality rule, with the reason.

   Rejected rows are never silently dropped. Every one lands here with the
   rule that caught it and the raw payload, so it can be explained.
   -------------------------------------------------------------------------- */
IF OBJECT_ID('ctl.Quarantine') IS NOT NULL DROP TABLE ctl.Quarantine;
GO
CREATE TABLE ctl.Quarantine (
    quarantine_id       BIGINT        IDENTITY(1,1) PRIMARY KEY,
    run_id              UNIQUEIDENTIFIER NULL REFERENCES ctl.PipelineRun(run_id),
    source_id           INT           NULL REFERENCES ctl.IngestionSource(source_id),
    target_table        NVARCHAR(200) NULL,
    rule_name           NVARCHAR(100) NOT NULL,
    reason_code         NVARCHAR(50)  NOT NULL,   -- NULL_KEY|BAD_DATE|ORPHAN_FK|NEG_AMOUNT|DUPLICATE
    source_file         NVARCHAR(400) NULL,
    row_payload         NVARCHAR(MAX) NULL,       -- the offending row as JSON
    quarantined_at      DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
CREATE INDEX IX_Quarantine_Run ON ctl.Quarantine (run_id, reason_code);
GO


/* ==========================================================================
   PROCEDURES
   ========================================================================== */

/* Called by ADF at the start of every source load. Returns the run_id and
   the window to load. The ceiling is frozen HERE, before any data moves.

   This ordering is the whole point. Read the ceiling after the copy and any
   row that arrives mid-run is skipped by the current run and excluded from
   the next one, because the watermark has already moved past it. Those rows
   are lost silently and forever. */
IF OBJECT_ID('ctl.usp_StartRun') IS NOT NULL DROP PROCEDURE ctl.usp_StartRun;
GO
CREATE PROCEDURE ctl.usp_StartRun
    @source_name        NVARCHAR(100),
    @adf_pipeline_name  NVARCHAR(200),
    @adf_run_id         NVARCHAR(100),
    @ceiling            NVARCHAR(100) = NULL      -- pass NULL for UTC now
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @source_id INT, @load_type NVARCHAR(20), @run_id UNIQUEIDENTIFIER = NEWID();

    SELECT @source_id = source_id, @load_type = load_type
    FROM   ctl.IngestionSource
    WHERE  source_name = @source_name AND is_active = 1;

    IF @source_id IS NULL
        THROW 50001, 'Unknown or inactive source in ctl.IngestionSource', 1;

    DECLARE @floor NVARCHAR(100) =
        (SELECT watermark_value FROM ctl.Watermark WHERE source_id = @source_id);

    IF @ceiling IS NULL
        SET @ceiling = CONVERT(NVARCHAR(100), SYSUTCDATETIME(), 126);

    INSERT INTO ctl.PipelineRun
        (run_id, adf_pipeline_name, adf_run_id, source_id, load_type,
         window_start, window_end, status)
    VALUES
        (@run_id, @adf_pipeline_name, @adf_run_id, @source_id, @load_type,
         @floor, @ceiling, 'RUNNING');

    SELECT @run_id      AS run_id,
           @source_id   AS source_id,
           @load_type   AS load_type,
           @floor       AS window_start,
           @ceiling     AS window_end;
END;
GO


/* Called on success or failure. On success it advances the watermark to the
   ceiling that was frozen at the start, never to "now". */
IF OBJECT_ID('ctl.usp_EndRun') IS NOT NULL DROP PROCEDURE ctl.usp_EndRun;
GO
CREATE PROCEDURE ctl.usp_EndRun
    @run_id             UNIQUEIDENTIFIER,
    @status             NVARCHAR(20),
    @files_read         INT           = NULL,
    @rows_read          BIGINT        = NULL,
    @rows_written       BIGINT        = NULL,
    @rows_quarantined   BIGINT        = NULL,
    @error_message      NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE ctl.PipelineRun
    SET    ended_at         = SYSUTCDATETIME(),
           status           = @status,
           files_read       = @files_read,
           rows_read        = @rows_read,
           rows_written     = @rows_written,
           rows_quarantined = @rows_quarantined,
           error_message    = @error_message
    WHERE  run_id = @run_id;

    IF @status = 'SUCCEEDED'
    BEGIN
        DECLARE @source_id INT, @ceiling NVARCHAR(100);
        SELECT @source_id = source_id, @ceiling = window_end
        FROM   ctl.PipelineRun WHERE run_id = @run_id;

        MERGE ctl.Watermark AS t
        USING (SELECT @source_id AS source_id) AS s ON t.source_id = s.source_id
        WHEN MATCHED THEN UPDATE SET
            previous_value  = t.watermark_value,
            watermark_value = @ceiling,
            last_updated    = SYSUTCDATETIME(),
            last_run_id     = @run_id
        WHEN NOT MATCHED THEN INSERT
            (source_id, watermark_value, last_updated, last_run_id)
            VALUES (@source_id, @ceiling, SYSUTCDATETIME(), @run_id);
    END
END;
GO


/* Rolls a source back one step. Useful when a load succeeded but the data
   was wrong, which is exactly when you least want to be editing tables by
   hand at speed. */
IF OBJECT_ID('ctl.usp_RollbackWatermark') IS NOT NULL DROP PROCEDURE ctl.usp_RollbackWatermark;
GO
CREATE PROCEDURE ctl.usp_RollbackWatermark
    @source_name NVARCHAR(100)
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE w
    SET    w.watermark_value = w.previous_value,
           w.previous_value  = NULL,
           w.last_updated    = SYSUTCDATETIME()
    FROM   ctl.Watermark w
    JOIN   ctl.IngestionSource s ON s.source_id = w.source_id
    WHERE  s.source_name = @source_name;
END;
GO


/* ==========================================================================
   SEED — the manifest

   18 sources. Every one of them lands in ADLS via the same pipeline.
   ========================================================================== */

INSERT INTO ctl.IngestionSource
    (source_name, source_group, platform_code, source_type, load_type,
     source_folder, file_pattern, file_format, column_delimiter, date_format,
     file_encoding, rest_base_url, rest_path_template, rest_iterator,
     landing_container, landing_folder,
     watermark_column, watermark_type, expected_min_rows, load_order)
VALUES
-- ---- Brazilian platform: static reference extracts, full load each time ----
 ('olist_orders',         'olist','BR','file','FULL', 'raw/olist','olist_orders_dataset.csv',              'csv',',','yyyy-MM-dd HH:mm:ss','UTF-8',NULL,NULL,NULL,'landing','bronze/olist/orders',        NULL,NULL, 99000,10),
 ('olist_order_items',    'olist','BR','file','FULL', 'raw/olist','olist_order_items_dataset.csv',         'csv',',','yyyy-MM-dd HH:mm:ss','UTF-8',NULL,NULL,NULL,'landing','bronze/olist/order_items',   NULL,NULL,112000,10),
 ('olist_customers',      'olist','BR','file','FULL', 'raw/olist','olist_customers_dataset.csv',           'csv',',',NULL,                 'UTF-8',NULL,NULL,NULL,'landing','bronze/olist/customers',     NULL,NULL, 99000,10),
 ('olist_products',       'olist','BR','file','FULL', 'raw/olist','olist_products_dataset.csv',            'csv',',',NULL,                 'UTF-8',NULL,NULL,NULL,'landing','bronze/olist/products',      NULL,NULL, 32000,10),
 ('olist_sellers',        'olist','BR','file','FULL', 'raw/olist','olist_sellers_dataset.csv',             'csv',',',NULL,                 'UTF-8',NULL,NULL,NULL,'landing','bronze/olist/sellers',       NULL,NULL,  3000,10),
 ('olist_order_payments', 'olist','BR','file','FULL', 'raw/olist','olist_order_payments_dataset.csv',      'csv',',',NULL,                 'UTF-8',NULL,NULL,NULL,'landing','bronze/olist/payments',      NULL,NULL,103000,10),
 ('olist_order_reviews',  'olist','BR','file','FULL', 'raw/olist','olist_order_reviews_dataset.csv',       'csv',',','yyyy-MM-dd HH:mm:ss','UTF-8',NULL,NULL,NULL,'landing','bronze/olist/reviews',       NULL,NULL, 98000,10),
 ('olist_geolocation',    'olist','BR','file','FULL', 'raw/olist','olist_geolocation_dataset.csv',         'csv',',',NULL,                 'UTF-8',NULL,NULL,NULL,'landing','bronze/olist/geolocation',   NULL,NULL,999000,10),
 ('olist_category_xlat',  'olist','BR','file','FULL', 'raw/olist','product_category_name_translation.csv', 'csv',',',NULL,                 'UTF-8',NULL,NULL,NULL,'landing','bronze/olist/category_xlat', NULL,NULL,    70,10),

-- ---- Merchant masters: monthly full snapshots, SCD2 source ----
 ('br_merchant_snapshot', 'merchant_master','BR','file','SNAPSHOT','generated/br_merchants','br_merchants_*.csv','csv',',','yyyy-MM-dd', 'UTF-8',NULL,NULL,NULL,'landing','bronze/merchants/br','snapshot_date','date',3000,20),
 ('za_vendor_snapshot',   'merchant_master','ZA','file','SNAPSHOT','generated/za_merchants','za_vendors_*.csv',  'csv',';','dd/MM/yyyy', 'UTF-8',NULL,NULL,NULL,'landing','bronze/merchants/za', 'snapshot_date','date', 800,20),

-- ---- South African platform: monthly incremental extracts ----
 ('za_orders',            'za_platform','ZA','file','INCREMENTAL','generated/za_orders',     'za_orders_*.csv',     'csv',',','dd/MM/yyyy HH:mm','UTF-8',NULL,NULL,NULL,'landing','bronze/za/orders',     'ord_dt',   'filename',30000,30),
 ('za_order_lines',       'za_platform','ZA','file','INCREMENTAL','generated/za_order_lines','za_order_lines_*.csv','csv',',',NULL,             'UTF-8',NULL,NULL,NULL,'landing','bronze/za/order_lines',NULL,       'filename',40000,30),
 ('za_customers',         'za_platform','ZA','file','INCREMENTAL','generated/za_customers',  'za_customers_*.csv',  'csv',',','dd/MM/yyyy',     'UTF-8',NULL,NULL,NULL,'landing','bronze/za/customers',  'signup_dt','filename',20000,30),
 ('za_refunds',           'za_platform','ZA','file','INCREMENTAL','generated/za_refunds',    'za_refunds_*.csv',    'csv',',','dd/MM/yyyy',     'UTF-8',NULL,NULL,NULL,'landing','bronze/za/refunds',    'refund_dt','filename',  900,40),

-- ---- APIs: 42 calls total across three sources ----
 ('fx_rates_ecb',   'api',NULL,'rest','FULL',NULL,NULL,'json',NULL,NULL,NULL,'https://api.frankfurter.dev/v1','/@{item().start}..@{item().end}?base=@{item().base}&symbols=USD','currency_pair','landing','bronze/reference/fx',      NULL,NULL,  900,50),
 ('holidays_nager', 'api',NULL,'rest','FULL',NULL,NULL,'json',NULL,NULL,NULL,'https://date.nager.at/api/v3',  '/PublicHolidays/@{item().year}/@{item().country}',                'country_year', 'landing','bronze/reference/holidays',NULL,NULL,   50,50),
 ('weather_openmeteo','api',NULL,'rest','FULL',NULL,NULL,'json',NULL,NULL,NULL,'https://archive-api.open-meteo.com/v1','/archive?latitude=@{item().lat}&longitude=@{item().lon}&start_date=@{item().start}&end_date=@{item().end}&daily=precipitation_sum,wind_speed_10m_max,temperature_2m_max&timezone=auto','region','landing','bronze/reference/weather',NULL,NULL,24000,50);
GO


/* ==========================================================================
   VERIFY
   ========================================================================== */
SELECT source_group,
       load_type,
       COUNT(*) AS sources,
       SUM(expected_min_rows) AS min_rows_expected
FROM   ctl.IngestionSource
WHERE  is_active = 1
GROUP BY source_group, load_type
ORDER BY MIN(load_order), source_group;

-- This is the exact query ADF's Lookup activity runs.
SELECT *
FROM   ctl.IngestionSource
WHERE  is_active = 1
ORDER BY load_order, source_id;
GO
