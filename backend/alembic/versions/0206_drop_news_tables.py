"""0206 — drop the news/content vertical's tables.

The optional news vertical (news crawler, shorts/storyboards, trend feed,
newsletter, video performance) was retired. Its models stayed in core only
so the Alembic chain was identical with and without the vertical; nothing
in this repository reads them any more.

Drops (data included — take a backup first if an install still holds any):
  news_sources, news_articles, news_post_schedules, newsletter_issues,
  storyboards, trend_signals, viral_shorts_settings, video_performance,
  plus ``news_config`` if present (created at runtime by the vertical, never
  by a migration).

``content_pipelines`` stays (core: bench drafts, X-post approvals); only its
``rss_source_id`` column goes, because its FK points into news_sources.

Downgrade recreates the schema exactly as 0099–0131 left it — empty. The
DDL below is a pg_dump of a fresh ``alembic upgrade 0205`` database.
``news_config`` is not recreated (no migration ever owned it).

Revision ID: 0206_drop_news_tables
Revises: 0205_model_usage_head_runs
"""
from alembic import op

revision = "0206_drop_news_tables"
down_revision = "0205_model_usage_head_runs"
branch_labels = None
depends_on = None

# Children before parents, no CASCADE: an unknown dependent must fail loudly.
NEWS_TABLES = (
    "video_performance",
    "storyboards",
    "trend_signals",
    "news_post_schedules",
    "news_articles",
    "news_sources",
    "newsletter_issues",
    "viral_shorts_settings",
)

_SCHEMA = """
CREATE TABLE news_articles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title character varying NOT NULL,
    summary text,
    content text,
    url character varying NOT NULL,
    source_id uuid,
    category character varying DEFAULT 'general'::character varying NOT NULL,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    published_at timestamp with time zone,
    scraped_at timestamp with time zone DEFAULT now() NOT NULL,
    image_url character varying,
    is_featured boolean DEFAULT false NOT NULL,
    is_posted boolean DEFAULT false NOT NULL,
    post_urls jsonb,
    engagement jsonb,
    slug character varying NOT NULL,
    post_worthy_score double precision,
    ai_summary text,
    ai_tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    status character varying DEFAULT 'new'::character varying NOT NULL,
    has_pipeline boolean DEFAULT false NOT NULL,
    published_by uuid,
    suggested_tweet text,
    suggested_linkedin text,
    approved_tweet text,
    approved_linkedin text,
    social_status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    viral_score_dimensions jsonb,
    viral_score_total double precision,
    hook_variants jsonb,
    pipeline_id uuid,
    ai_score double precision,
    ai_tweet text,
    ai_linkedin text,
    ai_category character varying,
    processed_at timestamp with time zone
);
CREATE TABLE news_post_schedules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    article_id uuid,
    platform character varying NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    content text NOT NULL,
    posted_at timestamp with time zone,
    error_log text,
    external_post_id character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE news_sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying NOT NULL,
    rss_url character varying NOT NULL,
    base_url character varying NOT NULL,
    category character varying DEFAULT 'tech'::character varying NOT NULL,
    reliability_score double precision DEFAULT '0.8'::double precision NOT NULL,
    crawl_interval integer DEFAULT 30 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_crawled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    board_id uuid
);
CREATE TABLE newsletter_issues (
    id uuid NOT NULL,
    week_start date NOT NULL,
    week_end date NOT NULL,
    subject character varying(255) NOT NULL,
    html_body text NOT NULL,
    md_body text,
    top_storyboard_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    status character varying(32) DEFAULT 'draft'::character varying NOT NULL,
    sent_at timestamp with time zone,
    recipient_count integer DEFAULT 0 NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE storyboards (
    id uuid NOT NULL,
    content_pipeline_id uuid NOT NULL,
    title character varying NOT NULL,
    topic_summary text,
    topic_cluster character varying,
    duration_s double precision DEFAULT '22'::double precision NOT NULL,
    status character varying DEFAULT 'draft'::character varying NOT NULL,
    beats jsonb DEFAULT '[]'::jsonb NOT NULL,
    voiceover_path character varying,
    alignment_path character varying,
    use_existing_voiceover boolean DEFAULT false NOT NULL,
    mp4_path character varying,
    props_json_path character varying,
    render_log text,
    approved_at timestamp with time zone,
    approved_by character varying,
    rejection_reason text,
    render_task_id uuid,
    diversity_fingerprint jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    silent_preview_url character varying(512),
    silent_preview_rendered_at timestamp with time zone,
    feedback_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    proposed_by_agent_id uuid,
    reasoning_md text,
    revision_count integer DEFAULT 0 NOT NULL,
    output_formats jsonb DEFAULT '["video"]'::jsonb NOT NULL,
    format_publish_status jsonb DEFAULT '{}'::jsonb NOT NULL,
    linkedin_post_md text,
    twitter_thread_md text,
    newsletter_block_md text,
    pinned_for_newsletter boolean DEFAULT false NOT NULL,
    linkedin_post_md_en text,
    twitter_thread_md_en text,
    newsletter_block_md_en text,
    languages jsonb DEFAULT '["de"]'::jsonb NOT NULL,
    image_url text,
    image_source character varying(32),
    image_alt_text character varying(512),
    image_prompt text
);
CREATE TABLE trend_signals (
    id uuid NOT NULL,
    source character varying NOT NULL,
    topic_keyword character varying NOT NULL,
    topic_cluster_id uuid,
    engagement_score double precision DEFAULT '0'::double precision NOT NULL,
    sample_post_text text,
    sample_post_url character varying,
    language character varying(10) DEFAULT 'en'::character varying NOT NULL,
    dach_relevance boolean DEFAULT false NOT NULL,
    related_news_id uuid,
    captured_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    extra_metadata jsonb,
    image_url text
);
CREATE TABLE video_performance (
    id uuid NOT NULL,
    storyboard_id uuid NOT NULL,
    platform character varying(32) NOT NULL,
    external_post_id character varying(128) NOT NULL,
    posted_at timestamp with time zone NOT NULL,
    views bigint DEFAULT 0 NOT NULL,
    likes bigint DEFAULT 0 NOT NULL,
    saves bigint DEFAULT 0 NOT NULL,
    shares bigint DEFAULT 0 NOT NULL,
    comments bigint DEFAULT 0 NOT NULL,
    watch_time_pct double precision,
    polled_at timestamp with time zone DEFAULT now() NOT NULL,
    performance_score double precision
);
CREATE TABLE viral_shorts_settings (
    id integer DEFAULT 1 NOT NULL,
    auto_publish_default boolean DEFAULT false NOT NULL,
    auto_publish_min_score integer DEFAULT 75 NOT NULL,
    daily_count integer DEFAULT 1 NOT NULL,
    cron_expression character varying DEFAULT '0 8 * * *'::character varying NOT NULL,
    cron_timezone character varying DEFAULT 'Europe/Berlin'::character varying NOT NULL,
    voice_id character varying,
    soul_id character varying,
    extra jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    newsletter_subscribers jsonb DEFAULT '[]'::jsonb NOT NULL,
    newsletter_sender_email character varying(255),
    newsletter_sender_name character varying(128),
    newsletter_resend_secret_id uuid,
    default_languages jsonb DEFAULT '["de"]'::jsonb NOT NULL,
    CONSTRAINT viral_shorts_settings_singleton CHECK ((id = 1))
);
ALTER TABLE ONLY news_articles
    ADD CONSTRAINT news_articles_pkey PRIMARY KEY (id);
ALTER TABLE ONLY news_post_schedules
    ADD CONSTRAINT news_post_schedules_pkey PRIMARY KEY (id);
ALTER TABLE ONLY news_sources
    ADD CONSTRAINT news_sources_pkey PRIMARY KEY (id);
ALTER TABLE ONLY newsletter_issues
    ADD CONSTRAINT newsletter_issues_pkey PRIMARY KEY (id);
ALTER TABLE ONLY storyboards
    ADD CONSTRAINT storyboards_content_pipeline_id_key UNIQUE (content_pipeline_id);
ALTER TABLE ONLY storyboards
    ADD CONSTRAINT storyboards_pkey PRIMARY KEY (id);
ALTER TABLE ONLY trend_signals
    ADD CONSTRAINT trend_signals_pkey PRIMARY KEY (id);
ALTER TABLE ONLY news_articles
    ADD CONSTRAINT uq_news_articles_url UNIQUE (url);
ALTER TABLE ONLY video_performance
    ADD CONSTRAINT video_performance_pkey PRIMARY KEY (id);
ALTER TABLE ONLY viral_shorts_settings
    ADD CONSTRAINT viral_shorts_settings_pkey PRIMARY KEY (id);
CREATE INDEX ix_news_articles_slug ON news_articles USING btree (slug);
CREATE INDEX ix_news_articles_social_status ON news_articles USING btree (social_status);
CREATE INDEX ix_news_articles_title ON news_articles USING btree (title);
CREATE INDEX ix_news_articles_url ON news_articles USING btree (url);
CREATE INDEX ix_news_sources_board_id ON news_sources USING btree (board_id);
CREATE INDEX ix_news_sources_name ON news_sources USING btree (name);
CREATE UNIQUE INDEX ix_newsletter_issues_week ON newsletter_issues USING btree (week_start, week_end);
CREATE INDEX ix_newsletter_issues_week_start ON newsletter_issues USING btree (week_start);
CREATE INDEX ix_storyboards_created_at ON storyboards USING btree (created_at);
CREATE INDEX ix_storyboards_pinned_for_newsletter ON storyboards USING btree (pinned_for_newsletter);
CREATE INDEX ix_storyboards_proposed_by_agent_id ON storyboards USING btree (proposed_by_agent_id);
CREATE INDEX ix_storyboards_status ON storyboards USING btree (status);
CREATE INDEX ix_storyboards_topic_cluster ON storyboards USING btree (topic_cluster);
CREATE INDEX ix_trend_signals_expires_at ON trend_signals USING btree (expires_at);
CREATE INDEX ix_trend_signals_source ON trend_signals USING btree (source);
CREATE INDEX ix_trend_signals_topic_keyword ON trend_signals USING btree (topic_keyword);
CREATE INDEX ix_video_performance_platform_external ON video_performance USING btree (platform, external_post_id);
CREATE INDEX ix_video_performance_polled_at ON video_performance USING btree (polled_at);
CREATE INDEX ix_video_performance_storyboard_id ON video_performance USING btree (storyboard_id);
ALTER TABLE ONLY news_articles
    ADD CONSTRAINT news_articles_published_by_fkey FOREIGN KEY (published_by) REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE ONLY news_articles
    ADD CONSTRAINT news_articles_source_id_fkey FOREIGN KEY (source_id) REFERENCES news_sources(id) ON DELETE SET NULL;
ALTER TABLE ONLY news_post_schedules
    ADD CONSTRAINT news_post_schedules_article_id_fkey FOREIGN KEY (article_id) REFERENCES news_articles(id) ON DELETE CASCADE;
ALTER TABLE ONLY news_sources
    ADD CONSTRAINT news_sources_board_id_fkey FOREIGN KEY (board_id) REFERENCES boards(id);
ALTER TABLE ONLY storyboards
    ADD CONSTRAINT storyboards_content_pipeline_id_fkey FOREIGN KEY (content_pipeline_id) REFERENCES content_pipelines(id) ON DELETE CASCADE;
ALTER TABLE ONLY storyboards
    ADD CONSTRAINT storyboards_proposed_by_agent_id_fkey FOREIGN KEY (proposed_by_agent_id) REFERENCES agents(id) ON DELETE SET NULL;
ALTER TABLE ONLY storyboards
    ADD CONSTRAINT storyboards_render_task_id_fkey FOREIGN KEY (render_task_id) REFERENCES tasks(id) ON DELETE SET NULL;
ALTER TABLE ONLY trend_signals
    ADD CONSTRAINT trend_signals_related_news_id_fkey FOREIGN KEY (related_news_id) REFERENCES news_articles(id) ON DELETE SET NULL;
ALTER TABLE ONLY video_performance
    ADD CONSTRAINT video_performance_storyboard_id_fkey FOREIGN KEY (storyboard_id) REFERENCES storyboards(id) ON DELETE CASCADE;
"""


def upgrade() -> None:
    op.execute("ALTER TABLE content_pipelines DROP COLUMN IF EXISTS rss_source_id")
    for table in NEWS_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP TABLE IF EXISTS news_config")


def downgrade() -> None:
    for statement in _SCHEMA.split(";\n"):
        if statement.strip():
            op.execute(statement.strip().rstrip(";"))
    op.execute("ALTER TABLE content_pipelines ADD COLUMN rss_source_id uuid")
    op.execute(
        "CREATE INDEX ix_content_pipelines_rss_source_id "
        "ON content_pipelines USING btree (rss_source_id)"
    )
    op.execute(
        "ALTER TABLE content_pipelines ADD CONSTRAINT content_pipelines_rss_source_id_fkey "
        "FOREIGN KEY (rss_source_id) REFERENCES news_sources(id)"
    )
