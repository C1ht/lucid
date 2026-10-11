use std::time::Duration;

use tauri::{Manager, Url};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

const BACKEND_PORT: u16 = 8799;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let app_handle = app.handle().clone();

            let config_dir = app_handle
                .path()
                .app_config_dir()
                .expect("no app config dir");
            let music_dir = app_handle
                .path()
                .audio_dir()
                .map(|d| d.join("Lucid"))
                .unwrap_or_else(|_| config_dir.join("music"));
            std::fs::create_dir_all(&config_dir).ok();
            std::fs::create_dir_all(&music_dir).ok();

            let resource_dir = app_handle
                .path()
                .resource_dir()
                .expect("no resource dir");
            let binaries_dir = resource_dir.join("binaries");
            let frontend_dir = resource_dir.join("frontend");

            let path_env = std::env::var("PATH").unwrap_or_default();
            // rip.exe and ffmpeg.exe both need to be reachable on PATH: our
            // backend shells out to `rip` by name, and rip itself shells out
            // to `ffmpeg` for conversion/muxing.
            let new_path = format!("{};{}", binaries_dir.display(), path_env);

            let (mut rx, child) = app_handle
                .shell()
                .sidecar("lucid-backend")
                .expect("failed to create sidecar command")
                .env("LUCID_CONFIG_DIR", config_dir.to_string_lossy().to_string())
                .env("LUCID_MUSIC_DIR", music_dir.to_string_lossy().to_string())
                .env("LUCID_FRONTEND_DIR", frontend_dir.to_string_lossy().to_string())
                .env("LUCID_RIP_BIN", binaries_dir.join("rip.exe").to_string_lossy().to_string())
                .env("PORT", BACKEND_PORT.to_string())
                .env("PATH", new_path)
                .spawn()
                .expect("failed to spawn backend sidecar");

            app_handle.manage(std::sync::Mutex::new(Some(child)));

            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        tauri_plugin_shell::process::CommandEvent::Stdout(line) => {
                            println!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                        tauri_plugin_shell::process::CommandEvent::Stderr(line) => {
                            eprintln!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                        _ => {}
                    }
                }
            });

            let app_handle2 = app_handle.clone();
            tauri::async_runtime::spawn(async move {
                let health_url = format!("http://127.0.0.1:{}/api/health", BACKEND_PORT);
                let client = reqwest::Client::new();
                let mut ready = false;
                for _ in 0..120 {
                    if let Ok(resp) = client.get(&health_url).send().await {
                        if resp.status().is_success() {
                            ready = true;
                            break;
                        }
                    }
                    tokio::time::sleep(Duration::from_millis(500)).await;
                }

                if let Some(window) = app_handle2.get_webview_window("main") {
                    if ready {
                        let url = Url::parse(&format!("http://127.0.0.1:{}/", BACKEND_PORT)).unwrap();
                        let _ = window.navigate(url);
                    } else {
                        let _ = window.eval(
                            "document.getElementById('status').textContent = 'Backend did not start in time.';",
                        );
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.app_handle().try_state::<std::sync::Mutex<Option<CommandChild>>>() {
                    if let Some(child) = state.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
