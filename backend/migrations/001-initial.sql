-- Initial demo schema comes from versioned SQLAlchemy metadata.
-- DOUBLE time/frequency parameters; JSON retains original points, no imputation.
-- Unique dataset/window/study names and report.run_id. Production requires reviewed ALTER migrations.
CREATE INDEX IF NOT EXISTS ix_spectral_runs_status ON spectral_runs(status);
-- Batch significance: batch + per-window frozen snapshot and empirical false-alarm probability.
-- significance_items has a unique (batch_id, study_id) so one batch cannot silently duplicate a window.
CREATE INDEX IF NOT EXISTS ix_significance_batches_status ON significance_batches(status);
