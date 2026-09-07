use std::{
    collections::HashSet,
    sync::{Arc, Mutex},
};

use chrono::Utc;

use crate::{
    config::{AppConfig, load_config, save_config},
    identity::IdentityManager,
    model::CheckResult,
};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Mutex<AppConfig>>,
    pub identity: Arc<IdentityManager>,
    pub results: Arc<Mutex<Vec<CheckResult>>>,
    pub tested_rules: Arc<Mutex<HashSet<String>>>,
}

impl AppState {
    pub fn load() -> Result<Self, String> {
        let mut config = load_config()?;
        let identity = Arc::new(IdentityManager::default());
        if config.connection_purge_pending || config.connection_expired(Utc::now()) {
            config.begin_connection_purge();
            save_config(&config)?;
            identity.delete()?;
            config.clear_connection();
            save_config(&config)?;
        } else if config.onboarding_completed && !identity.available() {
            config.clear_connection();
            save_config(&config)?;
        }
        Ok(Self {
            config: Arc::new(Mutex::new(config)),
            identity,
            results: Arc::new(Mutex::new(Vec::new())),
            tested_rules: Arc::new(Mutex::new(HashSet::new())),
        })
    }
}
