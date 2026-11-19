use thiserror::Error;

#[derive(Debug, Error)]
pub enum HeraldError {
    #[error("validation failed: {0}")]
    Validation(String),

    #[error("publish failed: {0}")]
    Publish(String),

    #[error("metrics collection failed: {0}")]
    Metrics(String),
}
