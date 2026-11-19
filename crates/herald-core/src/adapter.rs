use crate::{
    error::HeraldError,
    metrics::MetricsSnapshot,
    publish::{PublishRequest, PublishResult},
};

pub trait Adapter {
    fn validate(&self, request: &PublishRequest) -> Result<(), HeraldError>;

    fn publish(&self, request: PublishRequest) -> Result<PublishResult, HeraldError>;

    fn metrics(&self, post_id: &str) -> Result<MetricsSnapshot, HeraldError>;
}
