use crate::asset::Asset;

pub trait EligibilityRule {
    fn eligible(&self, asset: &Asset) -> bool;
}
