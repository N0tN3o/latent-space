/**
 * Latent Space — Independent Per-Page Internationalization (i18n)
 *
 * Key design rule:
 *   Every web interface (gallery, draw, kiosk, admin) manages its OWN isolated
 *   language state under `localStorage['ls_lang_' + pageId]` (with optional
 *   per-tab `?lang=en|hu` URL override). Switching language on one page NEVER
 *   changes the language of any other page.
 */
(function (global) {
    'use strict';

    const SUPPORTED = [
        { code: 'en', short: 'EN', label: 'English', locale: 'en-US' },
        { code: 'hu', short: 'HU', label: 'Magyar', locale: 'hu-HU' },
    ];
    const SUPPORTED_CODES = SUPPORTED.map((l) => l.code);

    const DICTIONARIES = {
        en: {
            // --- Shared ---
            'common.lang_label': 'Language',
            'common.gallery': 'Gallery',
            'common.draw': 'Gaze Draw',
            'common.kiosk': 'Print Kiosk',
            'common.refresh': 'Refresh',
            'common.cancel': 'Cancel',
            'common.save': 'Save',
            'common.loading': 'Loading...',
            'common.available': 'Available',
            'common.printed': 'Printed',
            'common.print': 'Print',
            'common.view': 'View',
            'common.delete': 'Delete',
            'common.today': 'Today',
            'common.all': 'All',
            'common.on': 'ON',
            'common.off': 'OFF',

            // --- Gallery (/) ---
            'gallery.page_title': 'Latent Space, Through the Eyes of the Algorithm',
            'gallery.heading': 'Latent Space, Through the Eyes of the Algorithm',
            'gallery.nav.prev': 'Prev',
            'gallery.nav.prev_title': 'Older Artwork (Left Arrow)',
            'gallery.nav.live': 'Live',
            'gallery.nav.live_title': 'Return to Live Feed',
            'gallery.nav.next': 'Next',
            'gallery.nav.next_title': 'Newer Artwork (Right Arrow)',
            'gallery.nav.draw': 'Gaze Draw',
            'gallery.nav.kiosk': 'Print Kiosk',
            'gallery.nav.print': 'Print',
            'gallery.section.analysis': 'Analysis',
            'gallery.section.poem': 'Poem',
            'gallery.section.attention': 'Attention Span',
            'gallery.badge.fractal': 'Fractal D={val}',
            'gallery.badge.entropy': 'Entropy={val}%',
            'gallery.qr.preparing': 'Preparing PDF...',
            'gallery.qr.generating': 'Generating digital print',
            'gallery.qr.scan': 'Scan to save your digital print',
            'gallery.placeholder.first_html': 'Waiting for the first gaze drawing — open <a href="/draw" style="color: #2a2a2a;">Gaze Draw</a> to begin.',
            'gallery.placeholder.first': 'Waiting for the first gaze drawing — open Gaze Draw to begin.',
            'gallery.placeholder.waiting': 'Waiting for drawing...',
            'gallery.placeholder.no_preview': 'No preview image archived for this print.',
            'gallery.waiting_data': 'Waiting for data...',
            'gallery.error.title': 'Reading Failed',
            'gallery.error.default_msg': 'We could not complete your reading. Please try drawing again.',
            'gallery.error.date_fallback': 'Reading failed',
            'gallery.archive.prefix': 'Archive View ({idx}/{total}) — {date}',
            'gallery.archive.fallback_analysis': 'Archived gaze drawing memento.',
            'gallery.audio_unsupported': 'Your browser does not support the audio element.',
            'gallery.translation_fallback_note': 'Original English (Hungarian translation unavailable for this piece)',

            // --- Gaze Painter (/draw) ---
            'draw.page_title': 'Latent Space — Browser Gaze Painter',
            'draw.heading': 'Latent Space — Gaze Painter',
            'draw.mode_tag.hybrid': 'DIRECT',
            'draw.mode_tag.flow': 'FLOW',
            'draw.status.ready': 'Ready (Move eyes or mouse/touch to draw)',
            'draw.status.searching': 'Searching for gaze — Step in front of camera to begin',
            'draw.status.calibrating_start': 'Calibrating 0% — Look at the center target first',
            'draw.status.calibrating_center': 'Calibrating {pct}% — Look at the center target',
            'draw.status.calibrating_follow': 'Calibrating {pct}% — Follow the moving target',
            'draw.status.drawing_pointer': 'Drawing (Mouse / Touch)',
            'draw.status.cleared': 'Canvas cleared',
            'draw.status.cleared_searching': 'Canvas cleared — Searching for gaze',
            'draw.status.calib_required': 'Please complete eye calibration first (or click Recalibrate)',
            'draw.status.timed_remaining': 'Drawing ({mode})... {sec}s remaining',
            'draw.status.session_blank': 'Session ended (blank canvas)',
            'draw.status.sending': 'Sending drawing to AI pipeline...',
            'draw.status.queued': 'Queued (#{pos}) — Generating reading...',
            'draw.status.running': 'Analysis Pipeline Running — Generating reading...',
            'draw.status.upload_failed_code': 'Upload failed ({code}) — Ready to retry',
            'draw.status.upload_failed': 'Upload failed — Ready to retry',
            'draw.status.failed_code': 'Failed ({code}) — Ready to retry',
            'draw.status.ready_next_visitor': 'Artwork & Poem Ready on Gallery — Next visitor may begin',
            'draw.status.ready_redirect': 'Artwork & Poem Ready — Redirecting to Gallery...',
            'draw.status.timeout': 'Timed out waiting for reading',
            'draw.status.recentered': 'Cursor re-centered to current gaze',
            'draw.status.loading_mediapipe': 'Loading MediaPipe FaceLandmarker...',
            'draw.status.mouse_active': 'Mouse / Touch Drawing Active',
            'draw.btn.start_cam': 'Start Webcam Eye Tracking',
            'draw.btn.loading_cam': 'Loading Eye Tracker...',
            'draw.btn.cam_active': 'Webcam Active',
            'draw.btn.center': 'Center Cursor [Space]',
            'draw.btn.center_title': 'Press Spacebar anytime while drawing to re-center the cursor to your current gaze',
            'draw.btn.recalibrate': 'Recalibrate',
            'draw.btn.mode_hybrid': 'Mode: Stabilized Direct',
            'draw.btn.mode_flow': 'Mode: Gaze Flow (Steering)',
            'draw.btn.mode_title': 'Switch between Anti-Snap Direct mapping and Gaze Flow Steering',
            'draw.btn.session_15s': '15s Session',
            'draw.btn.clear': 'Clear',
            'draw.btn.submit': 'Submit & Analyze',
            'draw.btn.submitting': 'Uploading & Analyzing...',
            'draw.btn.gallery': 'Gallery',
            'draw.btn.kiosk': 'Kiosk',
            'draw.brush.title': 'Brush Ink Style',
            'draw.brush.classic': 'Classic Obsidian Ink',
            'draw.brush.ember': 'Velocity Ember',
            'draw.brush.indigo': 'Midnight Indigo',
            'draw.helper_html': 'Tip: Press <strong>Spacebar</strong> while drawing to re-center the cursor to your current gaze (especially helpful when wearing glasses).',
            'draw.pip.title': 'Eye Tracker Feed',
            'draw.calib.step1': 'Step 1/2: Look steadily at the center target',
            'draw.calib.step2': 'Step 2/2: Follow the moving target with your eyes',
            'draw.calib.canvas_label': 'Calibrating Gaze ({pct}%) — {instruction}',
            'draw.alert.blank': 'Please draw something on the canvas first (using your eyes or mouse/touch).',
            'draw.err.default': 'Pipeline error occurred. Please try again.',
            'draw.err.upload': 'Error submitting drawing: {err}',
            'draw.err.lost': 'Server restarted or lost track of this submission. Please retry.',
            'draw.err.reading_failed': 'We could not complete your reading ({msg}). Your drawing is preserved — click Submit to retry or Clear/Recalibrate.',
            'draw.err.timeout': 'Analysis is taking longer than expected. Your drawing is preserved — retry or check the Gallery.',
            'draw.err.webcam': 'Webcam eye tracker could not load (check camera permissions or internet / local /vendor/mediapipe/ bundle). Mouse and touch drawing are ready.',

            // --- Print Kiosk (/kiosk) ---
            'kiosk.page_title': 'Latent Space — Print Kiosk',
            'kiosk.heading': 'Latent Space — Print Kiosk',
            'kiosk.nav.gallery': 'Gallery',
            'kiosk.nav.draw': 'Gaze Draw',
            'kiosk.btn.staff_unlock': 'Staff Unlock',
            'kiosk.btn.staff_unlocked': 'Staff Unlocked (Lock)',
            'kiosk.search_placeholder': "Search by time, date, 'today', 'yesterday'...",
            'kiosk.btn.today': 'Today',
            'kiosk.btn.all': 'All',
            'kiosk.btn.refresh': 'Refresh',
            'kiosk.autoprint_label': 'Auto-Print New Drawings',
            'kiosk.autoprint_title': 'Automatically send new drawings to the printer as soon as they are created',
            'kiosk.loading_prints': 'Loading available prints...',
            'kiosk.queue_count': 'Showing {shown} of {total} drawings',
            'kiosk.empty_filtered': 'No drawings match your filter.',
            'kiosk.thumb_placeholder': 'PDF Memento',
            'kiosk.btn.print_sheet': 'Print Sheet',
            'kiosk.btn.host_print': 'Host Print',
            'kiosk.btn.host_print_title': "Print directly on the server's connected printer",
            'kiosk.footer_hint': 'Select any artwork to open or print its full A4 memento sheet.',
            'kiosk.modal.title': 'Staff Unlock Required',
            'kiosk.modal.desc_html': 'Enter the Admin passcode (printed at server startup or in <code>config/admin_pin.txt</code>) to enable host printing and auto-print controls.',
            'kiosk.modal.placeholder': 'Admin passcode',
            'kiosk.modal.cancel': 'Cancel',
            'kiosk.modal.unlock': 'Unlock',
            'kiosk.err.auth_failed': 'Authentication failed',
            'kiosk.err.connection': 'Connection error: {err}',
            'kiosk.err.autoprint_update': 'Failed to update auto-print setting: {err}',
            'kiosk.alert.host_print_ok': 'Sent print job to host printer.',
            'kiosk.alert.host_print_err': 'Host printer error: {err}',

            // --- Admin (/admin) ---
            'admin.page_title': 'Latent Space — Command Center',
            'admin.auth.title': 'Latent Space Admin',
            'admin.auth.desc_html': 'Enter the admin passcode (printed at server startup, set via <code>ADMIN_PASSWORD</code>, or stored in <code>config/admin_pin.txt</code>).',
            'admin.auth.label': 'Admin Passcode',
            'admin.auth.submit': 'Unlock Command Center',
            'admin.auth.expired': 'Session expired or authentication required.',
            'admin.auth.invalid': 'Invalid passcode.',
            'admin.header.subtitle': 'Command Center',
            'admin.header.gallery': 'Gallery',
            'admin.header.draw': 'Browser Gaze Draw',
            'admin.header.kiosk': 'Web Print Kiosk',
            'admin.header.connecting': 'Connecting...',
            'admin.header.updated': 'Updated: {time}',
            'admin.header.logout': 'Sign Out',
            'admin.tab.monitor': 'Monitor',
            'admin.tab.ai': 'AI & Vision',
            'admin.tab.attributions': 'Attributions',
            'admin.tab.files': 'Prints & Auto-Print',
            'admin.tab.logs': 'Logs',
            'admin.monitor.title': 'System & Pipeline Overview',
            'admin.monitor.run_btn': 'Run Analysis on Current Drawing',
            'admin.monitor.running_btn': 'Starting Pipeline...',
            'admin.monitor.stop_ollama': 'Stop / Unload Ollama',
            'admin.monitor.stopping_ollama': 'Stopping Ollama...',
            'admin.monitor.kpi_pipeline': 'Pipeline Status',
            'admin.monitor.kpi_ai': 'Active AI Models',
            'admin.monitor.kpi_prints': 'Prints (Today / Total)',
            'admin.monitor.kpi_attributions': 'Attributions',
            'admin.monitor.kpi_attr_hint': 'Switch in Attributions tab',
            'admin.monitor.loading': 'Loading system status...',
            'admin.monitor.modules': 'Active Modules & Endpoints',
            'admin.monitor.resources': 'Host Resource Utilization ({device})',
            'admin.monitor.cpu': 'CPU Usage',
            'admin.monitor.mem': 'Memory Usage',
            'admin.monitor.disk': 'Storage Used',
            'admin.monitor.temp': 'Device Temp',
            'admin.monitor.translation': 'Translation Engine',
            'admin.monitor.pipe_ready': 'Ready',
            'admin.monitor.pipe_running': 'Running',
            'admin.monitor.pipe_running_q': 'Running (+{q} queued)',
            'admin.monitor.pipe_queued': 'Queued ({q})',
            'admin.monitor.pipe_error': 'Error',
            'admin.monitor.pipe_idle': 'Idle',
            'admin.monitor.pipe_last_run': 'Last run: {time} ({status})',
            'admin.monitor.pipe_status_q': 'Status: {status} | Queue: {q}',
            'admin.monitor.autoprint_on': 'Auto-Print: ON',
            'admin.monitor.autoprint_off': 'Auto-Print: OFF',
            'admin.monitor.attr_hidden': 'OFF (Hidden)',
            'admin.monitor.lbl_analysis': 'Analysis:',
            'admin.monitor.lbl_poem': 'Poem:',
            'admin.monitor.lbl_perception': 'Perception: {mode} ({model})',
            'admin.monitor.svc.web_server': 'Web Server',
            'admin.monitor.svc.analysis_watcher': 'Analysis Watcher',
            'admin.monitor.svc.ollama_server': 'Ollama Server',
            'admin.monitor.svc.desktop_gaze_painter': 'Desktop Gaze Painter',
            'admin.monitor.svc.browser_gaze_painter': 'Browser Gaze Painter',
            'admin.monitor.svc.web_print_kiosk': 'Web Print Kiosk',
            'admin.ai.title': 'Vision, Language & Translation Configuration',
            'admin.ai.subtitle': 'Configure your Vision perception model, Psychoanalyst LLM, Poet LLM, and EN → HU Translation engine independently.',
            'admin.ai.reload': 'Reload Models',
            'admin.ai.save_all': 'Apply & Save All',
            'admin.ai.vision_title': '0. Vision & Perception Model',
            'admin.ai.vision_sub': '(Drawing Interpreter)',
            'admin.ai.vision_stage': 'Stage: visual perception',
            'admin.ai.vision_mode': 'Vision Perception Mode',
            'admin.ai.badge_style': 'PDF & Gallery Badge Style',
            'admin.ai.badge_vision_only': 'Vision Archetype Only (e.g. SNAKE)',
            'admin.ai.badge_combined': 'Combined: Vision · Geometry (e.g. SNAKE · ELECTRIC DRIFT)',
            'admin.ai.badge_geometry_only': 'Geometry Archetype Only (vision still guides LLM)',
            'admin.ai.vision_ollama_model': 'Ollama Vision Model',
            'admin.ai.vision_temp': 'Temp (0–2)',
            'admin.ai.vision_max_tokens': 'Max Tokens',
            'admin.ai.vision_timeout': 'Timeout (sec)',
            'admin.ai.vision_clean_label': 'Auto-clean & filter archetype badge (strips numbering & filler sentences)',
            'admin.ai.vision_include_desc': 'Run 2nd pass for detailed visual description',
            'admin.ai.vision_label_prompt': 'Archetype / Pareidolia Prompt (Pass 1 — short 1–3 word badge)',
            'admin.ai.vision_desc_prompt': 'Visual Description Prompt (Pass 2 — fed to Psychoanalyst LLM)',
            'admin.ai.vision_zs_arch': 'Custom Rorschach Archetype Concepts (optional, comma or newline separated)',
            'admin.ai.vision_zs_tones': 'Custom Emotional Tone Concepts (optional, comma or newline separated)',
            'admin.ai.vision_resnet_topk': 'Top-K ImageNet Associations (1–5)',
            'admin.ai.apply_vision': 'Apply Vision Settings',
            'admin.ai.test_vision': 'Test Vision on Latest Drawing',
            'admin.ai.reset_vision': 'Reset Prompts to Default',
            'admin.ai.tts_toggle': 'Enable Audio / Spoken Poem Generation (TTS)',
            'admin.ai.tts_desc': 'Optional: leave OFF to skip audio synthesis completely. Audio errors are non-critical and never block readings.',
            'admin.ai.tts_provider': 'TTS Provider',
            'admin.ai.tts_voice1': 'Primary Voice ID (ElevenLabs Voice 1 or Google Cloud Voice Name)',
            'admin.ai.tts_voice2': 'Secondary Harmony Voice ID (ElevenLabs Voice 2)',
            'admin.ai.tts_save': 'Save TTS',
            'admin.ai.analysis_title': '1. Analysis Model',
            'admin.ai.analysis_sub': '(Psychoanalyst)',
            'admin.ai.poem_title': '2. Poem Model',
            'admin.ai.poem_sub': '(Minimalist Haiku Poet)',
            'admin.ai.presets_analysis': 'Quick Presets (Analysis):',
            'admin.ai.presets_poem': 'Quick Presets (Poem):',
            'admin.ai.provider': 'AI Provider',
            'admin.ai.model_id': 'Model Name / ID',
            'admin.ai.base_url': 'Base URL (Ollama / OpenAI)',
            'admin.ai.api_key': 'API Key',
            'admin.ai.api_key_placeholder': 'Leave blank to keep existing',
            'admin.ai.clear_api_key': 'Clear saved API key',
            'admin.ai.failover_provider': 'Failover Provider',
            'admin.ai.failover_model': 'Failover Model',
            'admin.ai.failover_placeholder': '(Optional override)',
            'admin.ai.assistant_name': 'Assistant Name',
            'admin.ai.max_words': 'Max Words (±10%)',
            'admin.ai.sys_prompt_analysis': 'System Prompt / Instructions (Analysis)',
            'admin.ai.sys_prompt_poem': 'System Prompt / Instructions (Poem / Haiku)',
            'admin.ai.advanced_persona': 'Advanced Persona Fields (Purpose, Expertise, Restrictions...)',
            'admin.ai.field_purpose': 'Purpose',
            'admin.ai.field_expertise': 'Expertise',
            'admin.ai.field_capabilities': 'Capabilities',
            'admin.ai.field_restrictions': 'Restrictions',
            'admin.ai.field_additional': 'Additional Instructions',
            'admin.ai.copy_to_poem': 'Copy Provider & Model from Analysis',
            'admin.ai.apply_analysis': 'Apply Analysis Model',
            'admin.ai.test_analysis': 'Test Analysis Model',
            'admin.ai.apply_poem': 'Apply Poem Model',
            'admin.ai.test_poem': 'Test Poem Model',
            'admin.tr.title': '3. Multilingual Translation (EN → HU)',
            'admin.tr.sub': '(Bilingual Psychoanalysis, Haiku & Archetype)',
            'admin.tr.badge': 'Stage: bilingual EN + HU',
            'admin.tr.enable': 'Enable Hungarian Bilingual Translation',
            'admin.tr.enable_hint': 'Saves both EN and HU per drawing so Gallery, Gaze Draw, Kiosk & Admin can each switch language independently.',
            'admin.tr.mode_label': 'Translation Engine',
            'admin.tr.mode_local': 'Local CPU NMT (Helsinki-NLP/opus-mt-tc-big-en-hu — 0 MB VRAM, ~1s overlapped)',
            'admin.tr.mode_ollama': 'Local Ollama Translator (e.g. translategemma:4b / gemma3:4b)',
            'admin.tr.mode_native': 'Native Multilingual LLM (Primary Analysis & Poem models write Hungarian directly)',
            'admin.tr.mode_off': 'Disabled (English only)',
            'admin.tr.print_lang_label': 'Default Auto-Print PDF Language',
            'admin.tr.print_lang_en': 'English (EN)',
            'admin.tr.print_lang_hu': 'Hungarian (HU)',
            'admin.tr.nmt_model': 'HuggingFace CPU NMT Model ID',
            'admin.tr.nmt_threads': 'CPU Threads Cap (1–16)',
            'admin.tr.nmt_preload': 'Preload CPU NMT model on server startup',
            'admin.tr.ollama_model': 'Ollama Translation Model',
            'admin.tr.ollama_temp': 'Temp (0–2)',
            'admin.tr.ollama_timeout': 'Timeout (sec)',
            'admin.tr.ollama_url': 'Ollama Base URL (optional override)',
            'admin.tr.native_note': 'Native Multilingual mode asks your configured Analysis and Poem LLM providers to author Hungarian psychoanalysis and haiku directly. Works best with gemma3:4b, jobautomation/OpenEuroLLM-Hungarian, qwen3:4b, Gemini, or Claude.',
            'admin.tr.apply': 'Apply Translation Settings',
            'admin.tr.preload': 'Download / Preload CPU Model',
            'admin.tr.preloading': 'Downloading / Loading CPU model...',
            'admin.tr.test': 'Test EN → HU Translation',
            'admin.tr.testing': 'Running EN → HU translation test...',
            'admin.tr.saved': 'Translation settings saved!',
            'admin.tr.status_checking': 'Checking translation model cache, Python packages, and Ollama models...',
            'admin.tr.banner_off_badge': 'Translation OFF',
            'admin.tr.banner_off_text': 'Readings will be stored in English only. UI pages can still switch EN / HU independently.',
            'admin.tr.banner_pkg_missing': 'Packages Missing',
            'admin.tr.banner_pkg_missing_text': 'Install torch, transformers, and sentencepiece in .venv.',
            'admin.tr.banner_hot_badge': 'CPU NMT Hot in RAM (0 MB VRAM)',
            'admin.tr.banner_hot_text': "Model '{model}' ready for ~1s overlapped EN → HU translation.",
            'admin.tr.banner_cached_badge': 'Cached Offline (0 MB VRAM)',
            'admin.tr.banner_cached_text': "Model '{model}' is cached on disk and ready for offline use.",
            'admin.tr.banner_dl_badge': 'Needs One-Time Download',
            'admin.tr.banner_dl_text': "Click 'Download / Preload CPU Model' below (or run python download_offline_assets.py --with-translation) to cache '{model}' locally (~350 MB).",
            'admin.tr.banner_ollama_ok': 'Ollama Model Installed',
            'admin.tr.banner_ollama_ok_text': "Local Ollama model '{model}' is ready. Note: on 8 GB VRAM GPUs, switching models after llama3.1:8b adds a brief swap delay.",
            'admin.tr.banner_ollama_missing': 'Not Pulled Locally',
            'admin.tr.banner_native_badge': 'Native Multilingual LLM',
            'admin.tr.banner_native_text': 'Uses your configured Analysis & Poem models ({provider} / {model}) to compose Hungarian natively.',
            'admin.attr.title': 'Attributions & Community Branding',
            'admin.attr.subtitle': 'Independently toggle Veszprém Vibe and/or Kasboek logos and attribution links on the Gallery, Print Kiosk, and PDF prints.',
            'admin.attr.save': 'Save Attribution Settings',
            'admin.attr.preview': 'Live Gallery & Print Footer Preview',
            'admin.attr.vv_enable': 'Enable Veszprém Vibe Attribution',
            'admin.attr.vv_community': 'Community',
            'admin.attr.vv_show_logo': 'Display Veszprém Vibe SVG Logo alongside attribution text',
            'admin.attr.vv_preset': 'Logo Variant Preset',
            'admin.attr.logo_path': 'Logo Path / URL',
            'admin.attr.intro_text': 'Intro Text',
            'admin.attr.link_prefix': 'Link Prefix',
            'admin.attr.web_label': 'Website Label',
            'admin.attr.web_url': 'Website URL',
            'admin.attr.li_label': 'LinkedIn Label',
            'admin.attr.li_url': 'LinkedIn URL',
            'admin.attr.fb_label': 'Facebook Label',
            'admin.attr.fb_url': 'Facebook URL',
            'admin.attr.custom_note': 'Custom Note (Optional)',
            'admin.attr.kas_enable': 'Enable Kasboek Attributions',
            'admin.attr.kas_show_logo': 'Display Kasboek Logo alongside attribution text',
            'admin.attr.learn_more_prefix': '"Learn More" Prefix',
            'admin.attr.kas_web_label': 'Website Link Label',
            'admin.attr.kas_web_url': 'Kasboek Website URL',
            'admin.attr.kas_li_label': 'Company LinkedIn Label',
            'admin.attr.kas_li_url': 'Kasboek LinkedIn URL',
            'admin.attr.thanks_prefix': 'Founders / Owners Thank-You Prefix',
            'admin.attr.founders_heading': 'CEO / Owner / Co-Founders (1–3 LinkedIn Links)',
            'admin.attr.custom_footer_note': 'Custom Footer Note (Optional)',
            'admin.attr.preview_off_html': '<em>Both Attribution Switches are <strong>OFF</strong>.<br>No logos or attribution text will be shown on the Gallery or Prints.</em>',
            'admin.files.autoprint': 'Auto-Print Every New Drawing',
            'admin.files.autoprint_desc': 'Automatically sends new PDFs to the host printer without needing a kiosk screen.',
            'admin.files.printer_name': 'Host Printer Name',
            'admin.files.paper_format': 'Paper Format',
            'admin.files.save_print': 'Save Print Config',
            'admin.files.qr_url_label': 'Public / QR Base URL',
            'admin.files.qr_url_hint': '(Optional — overrides auto-detected LAN IP on Gallery & PDF QR codes)',
            'admin.files.save_qr_url': 'Save Public QR URL',
            'admin.files.refresh': 'Refresh List',
            'admin.files.open_kiosk': 'Open Tablet/Web Kiosk (/kiosk)',
            'admin.files.prune': 'Prune Old Prints (Older than today)',
            'admin.files.col_preview': 'Preview',
            'admin.files.col_file': 'Filename',
            'admin.files.col_date': 'Date Created',
            'admin.files.col_archetype': 'Archetype',
            'admin.files.col_langs': 'Languages',
            'admin.files.col_status': 'Status',
            'admin.files.col_size': 'Size',
            'admin.files.col_actions': 'Actions',
            'admin.files.empty': 'No print files found',
            'admin.files.confirm_prune': 'Delete ALL print files older than today?',
            'admin.files.confirm_delete': 'Delete {filename}?',
            'admin.files.print_sent': 'Print job sent to host printer.',
            'admin.files.attr_saved': 'Attribution settings saved and live on the Gallery.',
            'admin.files.qr_saved': 'Public QR Base URL saved.',
            'admin.logs.pipeline': 'Pipeline Log (installation.log)',
            'admin.logs.vision': 'Vision Perception Log (vision.log)',
            'admin.logs.responses': 'AI Responses Log',
            'admin.logs.predictions': 'Archetype Predictions (CSV)',
            'admin.logs.credits': 'TTS Credits (credits.csv)',
            'admin.logs.placeholder': 'Select a log file above to view detailed system output...',
        },

        hu: {
            // --- Közös ---
            'common.lang_label': 'Nyelv',
            'common.gallery': 'Galéria',
            'common.draw': 'Rajzolás',
            'common.kiosk': 'Nyomtatópult',
            'common.refresh': 'Frissítés',
            'common.cancel': 'Mégse',
            'common.save': 'Mentés',
            'common.loading': 'Betöltés…',
            'common.available': 'Elérhető',
            'common.printed': 'Kinyomtatva',
            'common.print': 'Nyomtatás',
            'common.view': 'Megnyitás',
            'common.delete': 'Törlés',
            'common.today': 'Ma',
            'common.all': 'Összes',
            'common.on': 'BE',
            'common.off': 'KI',

            // --- Galéria (/) ---
            'gallery.page_title': 'Latent Space — Az algoritmus szemével',
            'gallery.heading': 'Latent Space — Az algoritmus szemével',
            'gallery.nav.prev': 'Előző',
            'gallery.nav.prev_title': 'Korábbi alkotás (balra nyíl)',
            'gallery.nav.live': 'Élő',
            'gallery.nav.live_title': 'Vissza az élő nézethez',
            'gallery.nav.next': 'Következő',
            'gallery.nav.next_title': 'Újabb alkotás (jobbra nyíl)',
            'gallery.nav.draw': 'Rajzolás',
            'gallery.nav.kiosk': 'Nyomtatópult',
            'gallery.nav.print': 'Nyomtatás',
            'gallery.section.analysis': 'Elemzés',
            'gallery.section.poem': 'Vers',
            'gallery.section.attention': 'Figyelemtérkép',
            'gallery.badge.fractal': 'Fraktál D={val}',
            'gallery.badge.entropy': 'Entrópia={val}%',
            'gallery.qr.preparing': 'PDF készül…',
            'gallery.qr.generating': 'Digitális nyomat készül',
            'gallery.qr.scan': 'Olvasd be a digitális nyomatért',
            'gallery.placeholder.first_html': 'Még nincs elkészült alkotás — nyisd meg a <a href="/draw" style="color: #2a2a2a;">Rajzolás</a> oldalt a kezdéshez.',
            'gallery.placeholder.first': 'Még nincs elkészült alkotás — nyisd meg a Rajzolás oldalt a kezdéshez.',
            'gallery.placeholder.waiting': 'Várakozás a rajzra…',
            'gallery.placeholder.no_preview': 'Ehhez a nyomathoz nem tartozik mentett előnézeti kép.',
            'gallery.waiting_data': 'Várakozás az adatokra…',
            'gallery.error.title': 'Sikertelen elemzés',
            'gallery.error.default_msg': 'Nem sikerült elkészíteni az elemzést. Próbáld meg újra a rajzolást.',
            'gallery.error.date_fallback': 'Sikertelen elemzés',
            'gallery.archive.prefix': 'Archív nézet ({idx}/{total}) — {date}',
            'gallery.archive.fallback_analysis': 'Archivált szemkövetéses emléknyomat.',
            'gallery.audio_unsupported': 'A böngésződ nem támogatja a hanglejátszást.',
            'gallery.translation_fallback_note': 'Eredeti angol szöveg (ehhez az alkotáshoz nem készült magyar fordítás)',

            // --- Szemkövetős rajzoló (/draw) ---
            'draw.page_title': 'Latent Space — Szemkövetős rajzoló',
            'draw.heading': 'Latent Space — Szemkövetős rajzoló',
            'draw.mode_tag.hybrid': 'KÖZVETLEN',
            'draw.mode_tag.flow': 'IRÁNYÍTOTT',
            'draw.status.ready': 'Készen áll — rajzolj a szemeddel, egérrel vagy érintéssel',
            'draw.status.searching': 'Tekintet keresése — állj a kamera elé a kezdéshez',
            'draw.status.calibrating_start': 'Kalibrálás (0%) — először nézz a középső pontra',
            'draw.status.calibrating_center': 'Kalibrálás ({pct}%) — nézz a középső pontra',
            'draw.status.calibrating_follow': 'Kalibrálás ({pct}%) — kövesd a mozgó pontot',
            'draw.status.drawing_pointer': 'Rajzolás (egér / érintés)',
            'draw.status.cleared': 'Vászon letörölve',
            'draw.status.cleared_searching': 'Vászon letörölve — tekintet keresése',
            'draw.status.calib_required': 'Előbb fejezd be a kalibrálást (vagy kattints az Újrakalibrálásra)',
            'draw.status.timed_remaining': 'Rajzolás ({mode})… még {sec} mp',
            'draw.status.session_blank': 'Lejárt az idő (üres vászon)',
            'draw.status.sending': 'Rajz küldése elemzésre…',
            'draw.status.queued': 'Várólistán (#{pos}) — elemzés készül…',
            'draw.status.running': 'Elemzés folyamatban — értelmezés és vers készül…',
            'draw.status.upload_failed_code': 'Sikertelen feltöltés ({code}) — próbáld újra',
            'draw.status.upload_failed': 'Sikertelen feltöltés — próbáld újra',
            'draw.status.failed_code': 'Hiba ({code}) — próbáld újra',
            'draw.status.ready_next_visitor': 'Az alkotás megjelent a Galérián — jöhet a következő látogató',
            'draw.status.ready_redirect': 'Elkészült az alkotás — ugrás a Galériára…',
            'draw.status.timeout': 'Időtúllépés az elemzés közben',
            'draw.status.recentered': 'Kurzor középre igazítva',
            'draw.status.loading_mediapipe': 'Szemkövető modell betöltése…',
            'draw.status.mouse_active': 'Egér / érintéses rajzolás aktív',
            'draw.btn.start_cam': 'Szemkövetés indítása',
            'draw.btn.loading_cam': 'Szemkövető betöltése…',
            'draw.btn.cam_active': 'Kamera aktív',
            'draw.btn.center': 'Kurzor középre [Szóköz]',
            'draw.btn.center_title': 'Rajzolás közben bármikor nyomd meg a Szóköz billentyűt, hogy a kurzor visszaugorjon a tekinteted közepére',
            'draw.btn.recalibrate': 'Újrakalibrálás',
            'draw.btn.mode_hybrid': 'Mód: Stabilizált követés',
            'draw.btn.mode_flow': 'Mód: Folyamatos irányítás',
            'draw.btn.mode_title': 'Váltás a stabilizált közvetlen követés és a folyamatos tekintetirányítás között',
            'draw.btn.session_15s': '15 mp-es rajzolás',
            'draw.btn.clear': 'Törlés',
            'draw.btn.submit': 'Beküldés és elemzés',
            'draw.btn.submitting': 'Feltöltés és elemzés…',
            'draw.btn.gallery': 'Galéria',
            'draw.btn.kiosk': 'Nyomtatópult',
            'draw.brush.title': 'Ecset stílusa',
            'draw.brush.classic': 'Obszidián tinta',
            'draw.brush.ember': 'Sebességfüggő parázs',
            'draw.brush.indigo': 'Éjféli indigó',
            'draw.helper_html': 'Tipp: Rajzolás közben bármikor nyomd meg a <strong>Szóköz</strong> billentyűt, hogy a kurzor visszaugorjon a tekinteted közepére (szemüveggel különösen hasznos).',
            'draw.pip.title': 'Kamera élőképe',
            'draw.calib.step1': '1/2. lépés: Nézz mozdulatlanul a középső pontra',
            'draw.calib.step2': '2/2. lépés: Kövesd a mozgó pontot a szemeddel',
            'draw.calib.canvas_label': 'Szemkövetés kalibrálása ({pct}%) — {instruction}',
            'draw.alert.blank': 'Előbb rajzolj valamit a vászonra (a szemeddel, egérrel vagy érintéssel).',
            'draw.err.default': 'Feldolgozási hiba történt. Próbáld meg újra.',
            'draw.err.upload': 'Nem sikerült elküldeni a rajzot: {err}',
            'draw.err.lost': 'A szerver újraindult vagy megszakadt a feldolgozás. Próbáld meg újra.',
            'draw.err.reading_failed': 'Nem sikerült elkészíteni az elemzést ({msg}). A rajzod megmaradt — kattints a Beküldésre az újrapróbáláshoz, vagy töröld a vásznat.',
            'draw.err.timeout': 'Az elemzés a vártnál tovább tart. A rajzod megmaradt — próbáld újra, vagy nézd meg a Galériát.',
            'draw.err.webcam': 'A kamerás szemkövető nem tudott elindulni (ellenőrizd a kameraengedélyt vagy a helyi /vendor/mediapipe/ fájlokat). Egérrel és érintéssel továbbra is rajzolhatsz.',

            // --- Nyomtatópult (/kiosk) ---
            'kiosk.page_title': 'Latent Space — Nyomtatópult',
            'kiosk.heading': 'Latent Space — Nyomtatópult',
            'kiosk.nav.gallery': 'Galéria',
            'kiosk.nav.draw': 'Rajzolás',
            'kiosk.btn.staff_unlock': 'Személyzeti belépés',
            'kiosk.btn.staff_unlocked': 'Személyzeti mód aktív (Zárolás)',
            'kiosk.search_placeholder': 'Keresés idő, dátum, „ma” vagy „tegnap” alapján…',
            'kiosk.btn.today': 'Ma',
            'kiosk.btn.all': 'Összes',
            'kiosk.btn.refresh': 'Frissítés',
            'kiosk.autoprint_label': 'Új rajzok automatikus nyomtatása',
            'kiosk.autoprint_title': 'Minden elkészült rajz automatikusan a helyi nyomtatóra kerül',
            'kiosk.loading_prints': 'Elérhető nyomatok betöltése…',
            'kiosk.queue_count': '{shown} / {total} alkotás',
            'kiosk.empty_filtered': 'Nincs a szűrésnek megfelelő alkotás.',
            'kiosk.thumb_placeholder': 'PDF emléknyomat',
            'kiosk.btn.print_sheet': 'Nyomtatás',
            'kiosk.btn.host_print': 'Helyi nyomtatóra',
            'kiosk.btn.host_print_title': 'Közvetlen nyomtatás a szerverhez csatlakoztatott nyomtatón',
            'kiosk.footer_hint': 'Kattints bármelyik alkotásra a teljes A4-es emléknyomat megnyitásához vagy nyomtatásához.',
            'kiosk.modal.title': 'Személyzeti belépés',
            'kiosk.modal.desc_html': 'Add meg az admin jelszót (szerverindításkor kiírva vagy a <code>config/admin_pin.txt</code> fájlban) a közvetlen és automatikus nyomtatás engedélyezéséhez.',
            'kiosk.modal.placeholder': 'Admin jelszó',
            'kiosk.modal.cancel': 'Mégse',
            'kiosk.modal.unlock': 'Belépés',
            'kiosk.err.auth_failed': 'Hibás jelszó',
            'kiosk.err.connection': 'Kapcsolódási hiba: {err}',
            'kiosk.err.autoprint_update': 'Nem sikerült módosítani az automatikus nyomtatást: {err}',
            'kiosk.alert.host_print_ok': 'A nyomat elküldve a helyi nyomtatóra.',
            'kiosk.alert.host_print_err': 'Nyomtatási hiba: {err}',

            // --- Admin (/admin) ---
            'admin.page_title': 'Latent Space — Vezérlőpult',
            'admin.auth.title': 'Latent Space Admin',
            'admin.auth.desc_html': 'Add meg az admin jelszót (szerverindításkor kiírva, <code>ADMIN_PASSWORD</code> környezeti változóban vagy a <code>config/admin_pin.txt</code> fájlban).',
            'admin.auth.label': 'Admin jelszó',
            'admin.auth.submit': 'Belépés a vezérlőpultba',
            'admin.auth.expired': 'Lejárt a munkamenet, jelentkezz be újra.',
            'admin.auth.invalid': 'Hibás jelszó.',
            'admin.header.subtitle': 'Vezérlőpult',
            'admin.header.gallery': 'Galéria',
            'admin.header.draw': 'Rajzolás',
            'admin.header.kiosk': 'Nyomtatópult',
            'admin.header.connecting': 'Kapcsolódás…',
            'admin.header.updated': 'Frissítve: {time}',
            'admin.header.logout': 'Kijelentkezés',
            'admin.tab.monitor': 'Állapot',
            'admin.tab.ai': 'AI és látás',
            'admin.tab.attributions': 'Partnerek és logók',
            'admin.tab.files': 'Nyomatok és nyomtatás',
            'admin.tab.logs': 'Naplók',
            'admin.monitor.title': 'Rendszer és feldolgozás áttekintése',
            'admin.monitor.run_btn': 'Elemzés futtatása az aktuális rajzon',
            'admin.monitor.running_btn': 'Elemzés indítása…',
            'admin.monitor.stop_ollama': 'Ollama leállítása / VRAM kiürítése',
            'admin.monitor.stopping_ollama': 'Ollama leállítása…',
            'admin.monitor.kpi_pipeline': 'Feldolgozás állapota',
            'admin.monitor.kpi_ai': 'Aktív AI-modellek',
            'admin.monitor.kpi_prints': 'Nyomatok (ma / összes)',
            'admin.monitor.kpi_attributions': 'Partnerlogók',
            'admin.monitor.kpi_attr_hint': 'A Partnerek és logók fülön állítható',
            'admin.monitor.loading': 'Rendszerállapot betöltése…',
            'admin.monitor.modules': 'Aktív modulok és végpontok',
            'admin.monitor.resources': 'Erőforrás-használat ({device})',
            'admin.monitor.cpu': 'CPU-terhelés',
            'admin.monitor.mem': 'Memóriahasználat',
            'admin.monitor.disk': 'Tárhelyhasználat',
            'admin.monitor.temp': 'Hőmérséklet',
            'admin.monitor.translation': 'Fordítómotor',
            'admin.monitor.pipe_ready': 'Kész',
            'admin.monitor.pipe_running': 'Fut',
            'admin.monitor.pipe_running_q': 'Fut (+{q} várakozik)',
            'admin.monitor.pipe_queued': 'Várólistán ({q})',
            'admin.monitor.pipe_error': 'Hiba',
            'admin.monitor.pipe_idle': 'Tétlen',
            'admin.monitor.pipe_last_run': 'Utolsó futás: {time} ({status})',
            'admin.monitor.pipe_status_q': 'Állapot: {status} | Várólista: {q}',
            'admin.monitor.autoprint_on': 'Automatikus nyomtatás: BE',
            'admin.monitor.autoprint_off': 'Automatikus nyomtatás: KI',
            'admin.monitor.attr_hidden': 'KI (rejtve)',
            'admin.monitor.lbl_analysis': 'Elemzés:',
            'admin.monitor.lbl_poem': 'Vers:',
            'admin.monitor.lbl_perception': 'Látás: {mode} ({model})',
            'admin.monitor.svc.web_server': 'Webszerver',
            'admin.monitor.svc.analysis_watcher': 'Elemző figyelő',
            'admin.monitor.svc.ollama_server': 'Ollama szerver',
            'admin.monitor.svc.desktop_gaze_painter': 'Asztali szemkövető',
            'admin.monitor.svc.browser_gaze_painter': 'Böngészős rajzoló',
            'admin.monitor.svc.web_print_kiosk': 'Webes nyomtatópult',
            'admin.ai.title': 'Látás, nyelvi modellek és fordítás beállítása',
            'admin.ai.subtitle': 'A látásmodellt, a pszichoanalitikus LLM-et, a haikuköltőt és az angol → magyar fordítót egymástól függetlenül állíthatod be.',
            'admin.ai.reload': 'Modellek újratöltése',
            'admin.ai.save_all': 'Összes mentése',
            'admin.ai.vision_title': '0. Látásmodell',
            'admin.ai.vision_sub': '(Rajzértelmező)',
            'admin.ai.vision_stage': 'Szakasz: vizuális észlelés',
            'admin.ai.vision_mode': 'Látásfeldolgozás módja',
            'admin.ai.badge_style': 'Jelvény stílusa a Galérián és PDF-en',
            'admin.ai.badge_vision_only': 'Csak látásmodell archetípus (pl. KÍGYÓ)',
            'admin.ai.badge_combined': 'Kombinált: Látás · Geometria (pl. KÍGYÓ · ELEKTROMOS SODRÓDÁS)',
            'admin.ai.badge_geometry_only': 'Csak geometria archetípus (a látás továbbra is segíti az LLM-et)',
            'admin.ai.vision_ollama_model': 'Ollama látásmodell',
            'admin.ai.vision_temp': 'Hőmérséklet (0–2)',
            'admin.ai.vision_max_tokens': 'Max. token',
            'admin.ai.vision_timeout': 'Időkorlát (mp)',
            'admin.ai.vision_clean_label': 'Archetípus jelvény automatikus tisztítása (sorszámok és töltelékszöveg levágása)',
            'admin.ai.vision_include_desc': '2. kör futtatása részletes vizuális leíráshoz',
            'admin.ai.vision_label_prompt': 'Archetípus / pareidolia prompt (1. kör — 1–3 szavas jelvény)',
            'admin.ai.vision_desc_prompt': 'Vizuális leírás prompt (2. kör — az Elemző modellnek)',
            'admin.ai.vision_zs_arch': 'Egyéni Rorschach archetípus fogalmak (opcionális, vesszővel vagy sortöréssel)',
            'admin.ai.vision_zs_tones': 'Egyéni hangulat-fogalmak (opcionális, vesszővel vagy sortöréssel)',
            'admin.ai.vision_resnet_topk': 'Top-K ImageNet asszociáció (1–5)',
            'admin.ai.apply_vision': 'Látásbeállítások mentése',
            'admin.ai.test_vision': 'Látás tesztelése a legutóbbi rajzon',
            'admin.ai.reset_vision': 'Alapértelmezett promptok visszaállítása',
            'admin.ai.tts_toggle': 'Hangos versfelolvasás engedélyezése (TTS)',
            'admin.ai.tts_desc': 'Opcionális: hagyd kikapcsolva, ha nincs szükség hangfájlra. A hanggenerálás hibája sosem állítja meg az elemzést.',
            'admin.ai.tts_provider': 'TTS szolgáltató',
            'admin.ai.tts_voice1': 'Elsődleges hang ID (ElevenLabs Voice 1 vagy Google Cloud hangnév)',
            'admin.ai.tts_voice2': 'Másodlagos kísérőhang ID (ElevenLabs Voice 2)',
            'admin.ai.tts_save': 'TTS mentése',
            'admin.ai.analysis_title': '1. Elemző modell',
            'admin.ai.analysis_sub': '(Pszichoanalitikus)',
            'admin.ai.poem_title': '2. Versmodell',
            'admin.ai.poem_sub': '(Minimalista haikuköltő)',
            'admin.ai.presets_analysis': 'Gyorsbeállítások (Elemzés):',
            'admin.ai.presets_poem': 'Gyorsbeállítások (Vers):',
            'admin.ai.provider': 'AI szolgáltató',
            'admin.ai.model_id': 'Modell neve / azonosítója',
            'admin.ai.base_url': 'Alap URL (Ollama / OpenAI)',
            'admin.ai.api_key': 'API-kulcs',
            'admin.ai.api_key_placeholder': 'Hagyd üresen a meglévő megtartásához',
            'admin.ai.clear_api_key': 'Mentett API-kulcs törlése',
            'admin.ai.failover_provider': 'Tartalék szolgáltató',
            'admin.ai.failover_model': 'Tartalék modell',
            'admin.ai.failover_placeholder': '(Opcionális felülírás)',
            'admin.ai.assistant_name': 'Asszisztens neve',
            'admin.ai.max_words': 'Max. szavak (±10%)',
            'admin.ai.sys_prompt_analysis': 'Rendszerprompt / utasítások (Elemzés)',
            'admin.ai.sys_prompt_poem': 'Rendszerprompt / utasítások (Vers / Haiku)',
            'admin.ai.advanced_persona': 'Haladó szerepmezők (Cél, Szakértelem, Korlátozások…)',
            'admin.ai.field_purpose': 'Cél',
            'admin.ai.field_expertise': 'Szakértelem',
            'admin.ai.field_capabilities': 'Képességek',
            'admin.ai.field_restrictions': 'Korlátozások',
            'admin.ai.field_additional': 'További utasítások',
            'admin.ai.copy_to_poem': 'Szolgáltató és modell másolása az Elemzőtől',
            'admin.ai.apply_analysis': 'Elemző modell mentése',
            'admin.ai.test_analysis': 'Elemző modell tesztelése',
            'admin.ai.apply_poem': 'Versmodell mentése',
            'admin.ai.test_poem': 'Versmodell tesztelése',
            'admin.tr.title': '3. Többnyelvű fordítás (EN → HU)',
            'admin.tr.sub': '(Kétnyelvű elemzés, haiku és archetípus)',
            'admin.tr.badge': 'Szakasz: kétnyelvű EN + HU',
            'admin.tr.enable': 'Magyar kétnyelvű fordítás engedélyezése',
            'admin.tr.enable_hint': 'Minden rajzhoz elmenti az angol és magyar szöveget is, így a Galéria, Rajzolás, Nyomtatópult és Admin egymástól függetlenül váltható.',
            'admin.tr.mode_label': 'Fordítómotor',
            'admin.tr.mode_local': 'Helyi CPU NMT (Helsinki-NLP/opus-mt-tc-big-en-hu — 0 MB VRAM, ~1 mp átfedéssel)',
            'admin.tr.mode_ollama': 'Helyi Ollama fordító (pl. translategemma:4b / gemma3:4b)',
            'admin.tr.mode_native': 'Natív többnyelvű LLM (az Elemző és Versmodell közvetlenül magyarul ír)',
            'admin.tr.mode_off': 'Kikapcsolva (csak angol)',
            'admin.tr.print_lang_label': 'Alapértelmezett automatikus nyomtatási nyelv',
            'admin.tr.print_lang_en': 'Angol (EN)',
            'admin.tr.print_lang_hu': 'Magyar (HU)',
            'admin.tr.nmt_model': 'HuggingFace CPU NMT modell azonosító',
            'admin.tr.nmt_threads': 'CPU-szálkorlát (1–16)',
            'admin.tr.nmt_preload': 'CPU NMT modell előtöltése szerverindításkor',
            'admin.tr.ollama_model': 'Ollama fordítómodell',
            'admin.tr.ollama_temp': 'Hőmérséklet (0–2)',
            'admin.tr.ollama_timeout': 'Időkorlát (mp)',
            'admin.tr.ollama_url': 'Ollama alap URL (opcionális)',
            'admin.tr.native_note': 'Natív többnyelvű módban a beállított Elemző és Versmodell közvetlenül magyarul írja meg az elemzést és a haikut. Legjobban gemma3:4b, jobautomation/OpenEuroLLM-Hungarian, qwen3:4b, Gemini vagy Claude modellekkel működik.',
            'admin.tr.apply': 'Fordítási beállítások mentése',
            'admin.tr.preload': 'CPU modell letöltése / előtöltése',
            'admin.tr.preloading': 'CPU modell letöltése / betöltése…',
            'admin.tr.test': 'EN → HU fordítás tesztelése',
            'admin.tr.testing': 'EN → HU fordítási teszt futtatása…',
            'admin.tr.saved': 'Fordítási beállítások elmentve!',
            'admin.tr.status_checking': 'Fordítómodell gyorsítótár, Python-csomagok és Ollama-modellek ellenőrzése…',
            'admin.tr.banner_off_badge': 'Fordítás kikapcsolva',
            'admin.tr.banner_off_text': 'Az alkotások csak angolul mentődnek el. A felületek nyelve továbbra is külön váltható.',
            'admin.tr.banner_pkg_missing': 'Hiányzó csomagok',
            'admin.tr.banner_pkg_missing_text': 'Telepítsd a torch, transformers és sentencepiece csomagokat a .venv környezetbe.',
            'admin.tr.banner_hot_badge': 'CPU NMT memóriában (0 MB VRAM)',
            'admin.tr.banner_hot_text': 'A(z) „{model}” modell betöltve, készen áll a ~1 mp-es EN → HU fordításra.',
            'admin.tr.banner_cached_badge': 'Offline gyorsítótárazva (0 MB VRAM)',
            'admin.tr.banner_cached_text': 'A(z) „{model}” modell lementve a gépen, offline is azonnal használható.',
            'admin.tr.banner_dl_badge': 'Egyszeri letöltés szükséges',
            'admin.tr.banner_dl_text': 'Kattints a „CPU modell letöltése / előtöltése” gombra (vagy futtasd: python download_offline_assets.py --with-translation) a(z) „{model}” modell lementéséhez (~350 MB).',
            'admin.tr.banner_ollama_ok': 'Ollama modell telepítve',
            'admin.tr.banner_ollama_ok_text': 'A helyi „{model}” Ollama modell készen áll. Megjegyzés: 8 GB VRAM-os GPU-n a llama3.1:8b utáni modellváltás pár másodpercet hozzáad.',
            'admin.tr.banner_ollama_missing': 'Nincs letöltve helyben',
            'admin.tr.banner_native_badge': 'Natív többnyelvű LLM',
            'admin.tr.banner_native_text': 'A beállított Elemző és Versmodell ({provider} / {model}) közvetlenül magyarul írja meg a szövegeket.',
            'admin.attr.title': 'Partnerek és közösségi logók',
            'admin.attr.subtitle': 'A Veszprém Vibe és a Kasboek logók, illetve hivatkozások egymástól függetlenül kapcsolhatók a Galérián, a Nyomtatópulton és a PDF nyomatokon.',
            'admin.attr.save': 'Partnerbeállítások mentése',
            'admin.attr.preview': 'Élő előnézet (Galéria és PDF lábléc)',
            'admin.attr.vv_enable': 'Veszprém Vibe megjelenítés engedélyezése',
            'admin.attr.vv_community': 'Közösség',
            'admin.attr.vv_show_logo': 'Veszprém Vibe SVG logó megjelenítése a szöveg mellett',
            'admin.attr.vv_preset': 'Logóváltozat',
            'admin.attr.logo_path': 'Logó útvonala / URL',
            'admin.attr.intro_text': 'Bevezető szöveg',
            'admin.attr.link_prefix': 'Hivatkozás előtag',
            'admin.attr.web_label': 'Weboldal felirat',
            'admin.attr.web_url': 'Weboldal URL',
            'admin.attr.li_label': 'LinkedIn felirat',
            'admin.attr.li_url': 'LinkedIn URL',
            'admin.attr.fb_label': 'Facebook felirat',
            'admin.attr.fb_url': 'Facebook URL',
            'admin.attr.custom_note': 'Egyéni megjegyzés (opcionális)',
            'admin.attr.kas_enable': 'Kasboek megjelenítés engedélyezése',
            'admin.attr.kas_show_logo': 'Kasboek logó megjelenítése a szöveg mellett',
            'admin.attr.learn_more_prefix': '„Tudj meg többet” előtag',
            'admin.attr.kas_web_label': 'Weboldal felirat',
            'admin.attr.kas_web_url': 'Kasboek weboldal URL',
            'admin.attr.kas_li_label': 'Céges LinkedIn felirat',
            'admin.attr.kas_li_url': 'Kasboek LinkedIn URL',
            'admin.attr.thanks_prefix': 'Köszönetnyilvánítás előtag',
            'admin.attr.founders_heading': 'Alapítók / vezetők (1–3 LinkedIn-link)',
            'admin.attr.custom_footer_note': 'Egyéni láblécmegjegyzés (opcionális)',
            'admin.attr.preview_off_html': '<em>Mindkét partnerkapcsoló <strong>KI</strong> van kapcsolva.<br>Sem logó, sem partnerhivatkozás nem jelenik meg a Galérián és a nyomatokon.</em>',
            'admin.files.autoprint': 'Minden új rajz automatikus nyomtatása',
            'admin.files.autoprint_desc': 'Nyomtatópult nélkül, automatikusan a helyi nyomtatóra küldi az újonnan elkészült PDF-eket.',
            'admin.files.printer_name': 'Helyi nyomtató neve',
            'admin.files.paper_format': 'Papírméret',
            'admin.files.save_print': 'Nyomtatási beállítás mentése',
            'admin.files.qr_url_label': 'Publikus / QR alap URL',
            'admin.files.qr_url_hint': '(Opcionális — felülírja az automatikusan felismert LAN IP-címet a Galéria QR-kódjain)',
            'admin.files.save_qr_url': 'QR alap URL mentése',
            'admin.files.refresh': 'Lista frissítése',
            'admin.files.open_kiosk': 'Nyomtatópult megnyitása (/kiosk)',
            'admin.files.prune': 'Régi nyomatok törlése (mainál régebbiek)',
            'admin.files.col_preview': 'Előnézet',
            'admin.files.col_file': 'Fájlnév',
            'admin.files.col_date': 'Létrehozva',
            'admin.files.col_archetype': 'Archetípus',
            'admin.files.col_langs': 'Nyelvek',
            'admin.files.col_status': 'Állapot',
            'admin.files.col_size': 'Méret',
            'admin.files.col_actions': 'Műveletek',
            'admin.files.empty': 'Még nincsenek mentett nyomatok',
            'admin.files.confirm_prune': 'Biztosan törlöd az ÖSSZES mainál régebbi nyomatot?',
            'admin.files.confirm_delete': 'Biztosan törlöd ezt a fájlt: {filename}?',
            'admin.files.print_sent': 'A nyomat elküldve a helyi nyomtatóra.',
            'admin.files.attr_saved': 'A partnerbeállítások elmentve és azonnal aktívak a Galérián.',
            'admin.files.qr_saved': 'A publikus QR alap URL elmentve.',
            'admin.logs.pipeline': 'Feldolgozási napló (installation.log)',
            'admin.logs.vision': 'Látásnapló (vision.log)',
            'admin.logs.responses': 'AI-válaszok naplója',
            'admin.logs.predictions': 'Archetípus-predikciók (CSV)',
            'admin.logs.credits': 'TTS kreditek (credits.csv)',
            'admin.logs.placeholder': 'Válassz ki egy naplófájlt fent a részletes rendszerkimenet megtekintéséhez…',
        },
    };

    function normalizeLang(code) {
        const c = String(code || '').trim().toLowerCase().slice(0, 2);
        return SUPPORTED_CODES.includes(c) ? c : null;
    }

    function interpolate(template, vars) {
        if (!vars || typeof template !== 'string') return template;
        return template.replace(/\{(\w+)\}/g, (match, key) => {
            return Object.prototype.hasOwnProperty.call(vars, key) && vars[key] !== undefined && vars[key] !== null
                ? String(vars[key])
                : match;
        });
    }

    function pickLocalizedContent(data, lang) {
        const effLang = normalizeLang(lang) || 'en';
        const translations = (data && typeof data.translations === 'object' && data.translations) ? data.translations : {};
        const reqEntry = (translations[effLang] && typeof translations[effLang] === 'object') ? translations[effLang] : null;
        const enEntry = (translations.en && typeof translations.en === 'object') ? translations.en : null;

        const hasReq = !!(reqEntry && (reqEntry.poem || reqEntry.psychoanalysis || reqEntry.archetype));
        const src = hasReq ? reqEntry : (enEntry || data || {});
        return {
            archetype: src.archetype || (data && data.archetype) || '',
            psychoanalysis: src.psychoanalysis || (data && data.psychoanalysis) || '',
            poem: src.poem || (data && data.poem) || '',
            resolvedLang: hasReq ? effLang : 'en',
            isFallback: effLang !== 'en' && !hasReq && Boolean((data && (data.poem || data.psychoanalysis || data.archetype))),
        };
    }

    function getLocale(lang) {
        const meta = SUPPORTED.find((l) => l.code === lang);
        return meta ? meta.locale : 'en-US';
    }

    function formatDateTime(dateObj, lang) {
        if (!(dateObj instanceof Date) || isNaN(dateObj.getTime())) return '';
        try {
            return dateObj.toLocaleDateString(getLocale(lang), {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
            });
        } catch (_) {
            return dateObj.toISOString();
        }
    }

    function formatCardDate(dateObj, lang) {
        if (!(dateObj instanceof Date) || isNaN(dateObj.getTime())) return '';
        try {
            return dateObj.toLocaleDateString(getLocale(lang), {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
            });
        } catch (_) {
            return '';
        }
    }

    function formatCardTime(dateObj, lang) {
        if (!(dateObj instanceof Date) || isNaN(dateObj.getTime())) return '';
        try {
            return dateObj.toLocaleTimeString(getLocale(lang), {
                hour: '2-digit',
                minute: '2-digit',
            });
        } catch (_) {
            return '';
        }
    }

    function createPageController(pageId, options) {
        const opts = options || {};
        const storageKey = 'ls_lang_' + String(pageId || 'default').replace(/[^a-z0-9_-]/gi, '');
        const params = new URLSearchParams(global.location ? global.location.search : '');
        const urlLang = normalizeLang(params.get('lang'));
        const isPrintRender = params.get('print_render') === '1';

        let savedLang = null;
        if (!isPrintRender) {
            try {
                savedLang = normalizeLang(global.localStorage.getItem(storageKey));
            } catch (_) {}
        }

        let currentLang = urlLang || savedLang || normalizeLang(opts.defaultLang) || 'en';
        const switcherRefs = [];

        function t(key, vars) {
            const dict = DICTIONARIES[currentLang] || DICTIONARIES.en;
            const raw = Object.prototype.hasOwnProperty.call(dict, key)
                ? dict[key]
                : (Object.prototype.hasOwnProperty.call(DICTIONARIES.en, key) ? DICTIONARIES.en[key] : key);
            return interpolate(raw, vars);
        }

        function applyDOM(root) {
            const scope = root || global.document;
            if (!scope) return;
            if (global.document && global.document.documentElement) {
                global.document.documentElement.lang = currentLang;
            }
            if (opts.titleKey && global.document) {
                global.document.title = t(opts.titleKey);
            }
            scope.querySelectorAll('[data-i18n]').forEach((el) => {
                const k = el.getAttribute('data-i18n');
                if (k) el.textContent = t(k);
            });
            scope.querySelectorAll('[data-i18n-html]').forEach((el) => {
                const k = el.getAttribute('data-i18n-html');
                if (k) el.innerHTML = t(k);
            });
            scope.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
                const k = el.getAttribute('data-i18n-placeholder');
                if (k) el.setAttribute('placeholder', t(k));
            });
            scope.querySelectorAll('[data-i18n-title]').forEach((el) => {
                const k = el.getAttribute('data-i18n-title');
                if (k) el.setAttribute('title', t(k));
            });
            syncSwitchers();
        }

        function syncSwitchers() {
            switcherRefs.forEach((wrap) => {
                wrap.querySelectorAll('button[data-lang-code]').forEach((btn) => {
                    const active = btn.dataset.langCode === currentLang;
                    btn.classList.toggle('is-active', active);
                    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
                });
            });
        }

        function setLang(nextCode, skipCallback) {
            const norm = normalizeLang(nextCode);
            if (!norm) return currentLang;
            currentLang = norm;
            if (!isPrintRender) {
                try {
                    global.localStorage.setItem(storageKey, currentLang);
                } catch (_) {}
            }
            applyDOM();
            if (!skipCallback && typeof opts.onChange === 'function') {
                opts.onChange(currentLang);
            }
            return currentLang;
        }

        function mountSwitcher(container, styleVariant) {
            if (!container) return null;
            const variant = styleVariant || 'dark';
            const wrap = global.document.createElement('div');
            wrap.className = `ls-lang-switch ls-lang-${variant}`;
            wrap.setAttribute('role', 'group');
            wrap.setAttribute('aria-label', t('common.lang_label'));
            wrap.style.display = 'inline-flex';
            wrap.style.alignItems = 'center';
            wrap.style.borderRadius = '6px';
            wrap.style.overflow = 'hidden';
            wrap.style.border = variant === 'gallery' ? '1px solid #3a3a3a' : '1px solid #374151';

            SUPPORTED.forEach((langMeta) => {
                const btn = global.document.createElement('button');
                btn.type = 'button';
                btn.dataset.langCode = langMeta.code;
                btn.textContent = langMeta.short;
                btn.title = langMeta.label;
                btn.style.border = 'none';
                btn.style.cursor = 'pointer';
                btn.style.fontWeight = '700';
                btn.style.fontSize = variant === 'gallery' ? '0.72rem' : '0.78rem';
                btn.style.padding = variant === 'gallery' ? '5px 8px' : '6px 10px';
                btn.style.lineHeight = '1';
                btn.style.transition = 'background 0.15s, color 0.15s';
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (langMeta.code !== currentLang) {
                        setLang(langMeta.code);
                    }
                });
                wrap.appendChild(btn);
            });

            if (global.document && !global.document.getElementById('lsLangSwitchStyles')) {
                const st = global.document.createElement('style');
                st.id = 'lsLangSwitchStyles';
                st.textContent =
                    '.ls-lang-switch button { background: #1f2430; color: #9ca3af; }' +
                    '.ls-lang-switch button:hover { background: #2d3444; color: #fff; }' +
                    '.ls-lang-switch button.is-active { background: #10b981 !important; color: #ffffff !important; }' +
                    '.ls-lang-gallery button { background: #1f1f1f; color: #d1d5db; }' +
                    '.ls-lang-gallery button:hover { background: #3a3a3a; color: #fff; }';
                global.document.head.appendChild(st);
            }

            container.innerHTML = '';
            container.appendChild(wrap);
            switcherRefs.push(wrap);
            syncSwitchers();
            return wrap;
        }

        return {
            pageId: pageId,
            storageKey: storageKey,
            getLang: () => currentLang,
            setLang: setLang,
            t: t,
            applyDOM: applyDOM,
            mountSwitcher: mountSwitcher,
            pickContent: (data) => pickLocalizedContent(data, currentLang),
            formatDateTime: (d) => formatDateTime(d, currentLang),
            formatCardDate: (d) => formatCardDate(d, currentLang),
            formatCardTime: (d) => formatCardTime(d, currentLang),
        };
    }

    global.LS_I18N = {
        SUPPORTED: SUPPORTED,
        SUPPORTED_CODES: SUPPORTED_CODES,
        DICTIONARIES: DICTIONARIES,
        createPageController: createPageController,
        pickLocalizedContent: pickLocalizedContent,
        formatDateTime: formatDateTime,
        formatCardDate: formatCardDate,
        formatCardTime: formatCardTime,
    };
})(window);
