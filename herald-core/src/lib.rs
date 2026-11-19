//! Herald
//!
//! Content-agnostic distribution engine.
//!
//! Principles:
//! - Script as Law
//! - Illegal States Should Be Unrepresentable
//! - Fail and Bitch Loudly

pub mod adapter;
pub mod asset;
pub mod eligibility;
pub mod error;
pub mod metrics;
pub mod publish;

pub use adapter::Adapter;
pub use asset::Asset;
pub use error::HeraldError;
pub use metrics::MetricsSnapshot;
pub use publish::{PublishRequest, PublishResult};