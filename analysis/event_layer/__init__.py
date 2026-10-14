"""Provider-neutral soccer event enrichment."""

from .pipeline import PipelineConfig, PipelineResult, digest_events, run_pipeline

__all__ = ["PipelineConfig", "PipelineResult", "digest_events", "run_pipeline"]
__version__ = "0.1.0"
