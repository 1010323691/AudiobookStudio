//! AudiobookStudio desktop shell.
//!
//! The window hosts the Vue 3 frontend, which talks to the local Python
//! FastAPI backend at `http://127.0.0.1:8642`. Start the backend alongside
//! the shell (see `src-tauri/README.md`):
//!
//!     .venv/Scripts/python -m backend.main   # Windows
//!     python -m backend.main                 # macOS / Linux
//!
//! The two plugins registered here back the frontend's `@tauri-apps/plugin-*`
//! imports: `plugin-dialog` (native file picker) and `plugin-shell`
//! (open a file/folder in the OS).

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .run(tauri::generate_context!())
        .expect("error while running AudiobookStudio");
}
