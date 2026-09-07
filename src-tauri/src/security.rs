use std::{
    ffi::c_void,
    fs,
    io::Write,
    os::windows::ffi::OsStrExt,
    path::{Path, PathBuf},
    slice,
};

use windows::{
    Win32::{
        Foundation::{HLOCAL, LocalFree},
        Security::Cryptography::{
            CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN, CryptProtectData, CryptUnprotectData,
        },
        Storage::FileSystem::{MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW},
    },
    core::{PCWSTR, w},
};
use zeroize::Zeroize;

const ENTROPY: &[u8] = b"AzureHealthBeacon/v0.8/CurrentUser";

pub struct DpapiStore {
    path: PathBuf,
}

impl DpapiStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn exists(&self) -> bool {
        self.path.is_file()
    }

    pub fn save(&self, plaintext: &mut [u8]) -> Result<(), String> {
        let encrypted = protect(plaintext)?;
        plaintext.zeroize();
        let parent = self
            .path
            .parent()
            .ok_or("Identity path has no parent directory")?;
        fs::create_dir_all(parent)
            .map_err(|_| "Encrypted identity directory could not be created")?;
        let temporary = parent.join(format!("identity-{}.tmp", uuid::Uuid::new_v4()));
        let result = (|| {
            let mut file = fs::File::create(&temporary)
                .map_err(|_| "Encrypted identity file could not be created")?;
            file.write_all(&encrypted)
                .map_err(|_| "Encrypted identity file could not be written")?;
            file.sync_all()
                .map_err(|_| "Encrypted identity file could not be flushed")?;
            atomic_replace(&temporary, &self.path)
                .map_err(|_| "Encrypted identity file could not be replaced atomically")
        })();
        let _ = fs::remove_file(&temporary);
        result
    }

    pub fn load(&self) -> Result<Vec<u8>, String> {
        let encrypted =
            fs::read(&self.path).map_err(|_| "The encrypted Azure connection is missing")?;
        unprotect(&encrypted)
    }

    pub fn delete_parent(&self) -> Result<(), String> {
        let parent = self
            .path
            .parent()
            .ok_or("Identity path has no parent directory")?;
        if parent.exists() {
            fs::remove_dir_all(parent)
                .map_err(|_| "The encrypted Azure connection could not be deleted")?;
        }
        Ok(())
    }
}

pub(crate) fn atomic_replace(source: &Path, destination: &Path) -> Result<(), String> {
    let source_wide: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let destination_wide: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    unsafe {
        MoveFileExW(
            PCWSTR(source_wide.as_ptr()),
            PCWSTR(destination_wide.as_ptr()),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    }
    .map_err(|_| "Windows could not atomically replace the file".to_owned())
}

fn blob(bytes: &[u8]) -> CRYPT_INTEGER_BLOB {
    CRYPT_INTEGER_BLOB {
        cbData: bytes.len().try_into().unwrap_or(u32::MAX),
        pbData: bytes.as_ptr() as *mut u8,
    }
}

fn copy_and_free(output: CRYPT_INTEGER_BLOB) -> Result<Vec<u8>, String> {
    if output.pbData.is_null() || output.cbData == 0 {
        return Err("Windows credential encryption returned no data".into());
    }
    let copied = unsafe { slice::from_raw_parts(output.pbData, output.cbData as usize) }.to_vec();
    unsafe {
        let _ = LocalFree(Some(HLOCAL(output.pbData.cast::<c_void>())));
    }
    Ok(copied)
}

fn protect(plaintext: &[u8]) -> Result<Vec<u8>, String> {
    if plaintext.is_empty() {
        return Err("Refusing to persist an empty Azure connection".into());
    }
    let input = blob(plaintext);
    let entropy = blob(ENTROPY);
    let mut output = CRYPT_INTEGER_BLOB::default();
    unsafe {
        CryptProtectData(
            &input,
            w!("Azure Health Beacon v0.8"),
            Some(&entropy),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    }
    .map_err(|_| "Windows DPAPI CurrentUser encryption is unavailable")?;
    copy_and_free(output)
}

fn unprotect(encrypted: &[u8]) -> Result<Vec<u8>, String> {
    if encrypted.is_empty() {
        return Err("The encrypted Azure connection is empty".into());
    }
    let input = blob(encrypted);
    let entropy = blob(ENTROPY);
    let mut output = CRYPT_INTEGER_BLOB::default();
    unsafe {
        CryptUnprotectData(
            &input,
            None,
            Some(&entropy),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    }
    .map_err(|_| "The encrypted Azure connection could not be opened for this Windows user")?;
    copy_and_free(output)
}

pub fn remove_legacy_identity_after_migration(app_data: &Path) -> Result<(), String> {
    let legacy = app_data.join("identity");
    if legacy.exists() {
        fs::remove_dir_all(legacy)
            .map_err(|_| "The legacy encrypted identity cache could not be deleted")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dpapi_round_trip_has_no_plaintext_on_disk() {
        let directory = std::env::temp_dir().join(format!("beacon-dpapi-{}", uuid::Uuid::new_v4()));
        let store = DpapiStore::new(directory.join("identity.bin"));
        let mut secret = b"refresh-token-must-not-survive".to_vec();
        store.save(&mut secret).unwrap();
        assert!(secret.iter().all(|byte| *byte == 0));
        let disk = fs::read(directory.join("identity.bin")).unwrap();
        assert!(!disk.windows(13).any(|window| window == b"refresh-token"));
        let mut opened = store.load().unwrap();
        assert_eq!(opened, b"refresh-token-must-not-survive");
        opened.zeroize();
        store.delete_parent().unwrap();
    }
}
