import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = WEB / "assets"


class FrontendStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text()
        cls.css = (ASSETS / "styles.css").read_text()
        cls.api = (ASSETS / "api.js").read_text()
        cls.app = (ASSETS / "app.js").read_text()
        cls.chat_list = (ASSETS / "chat-list.js").read_text()
        cls.conversation = (ASSETS / "conversation.js").read_text()
        # Backends/models markup-building code moved here, and the pane's
        # orchestrator.html navigation moved here, in a later module split.
        cls.machines = (ASSETS / "machines.js").read_text()
        cls.orchestrator = (ASSETS / "orchestrator.js").read_text()
        cls.supervisor_list_js = (ASSETS / "orchestrator" / "list.js").read_text()
        cls.rail = (ASSETS / "orchestrator" / "rail.js").read_text()
        cls.tasks_js = (ASSETS / "orchestrator" / "tasks.js").read_text()
        cls.dom_js = (ASSETS / "orchestrator" / "dom.js").read_text()
        cls.supervisor_html = (WEB / "orchestrator.html").read_text()
        cls.banners_js = (ASSETS / "orchestrator" / "banners.js").read_text()
        cls.members_js = (ASSETS / "orchestrator" / "members.js").read_text()
        cls.list_js = (ASSETS / "orchestrator" / "list.js").read_text()
        cls.main_js_supervisor = (ASSETS / "orchestrator" / "main.js").read_text()
        # Not named in the plan's fixture list, but the new test below reads
        # self.stream_js and no prior test had loaded it -- without this the
        # test errors with AttributeError rather than failing its assertions.
        cls.stream_js = (ASSETS / "orchestrator" / "stream.js").read_text()

    def test_html_uses_external_assets(self):
        # Trailing quote omitted so a cache-busting ?v=N query stays valid. The
        # stylesheet kept its closing quote and so forbade the very thing the
        # line above allows -- which is why styles.css shipped unversioned and a
        # phone kept serving the previous one out of cache.
        self.assertIn('type="module" src="/assets/app.js', self.html)
        self.assertIn('rel="stylesheet" href="/assets/styles.css', self.html)
        self.assertNotIn("<style>", self.html)
        self.assertNotIn("async function sendMessage", self.html)
        self.assertNotIn("<script>\n", self.html)

    def test_machine_form_script_matches_markup(self):
        """Script and markup must agree on which machine inputs exist.

        Reading an input the markup does not define sends a field the form
        never collected, and handle_machine_patch rejects the whole body if
        any key falls outside its allowlist -- that is what broke every
        machine edit in 0.3.1. The invariant is agreement, not absence:
        machineBaseUrl is collected by the Anthropic provider form and must
        appear in both files.
        """
        # machineBaseUrl's collection code moved to machines.js in a later
        # module split; the negative checks still cover both files, since the
        # invariant is that neither one references a field the markup dropped.
        for element_id in ("machinePort", "machineDescription"):
            self.assertNotIn(element_id, self.html)
            self.assertNotIn(element_id, self.app)
            self.assertNotIn(element_id, self.machines)
        self.assertIn("machineBaseUrl", self.html)
        self.assertIn("machineBaseUrl", self.machines)

    def test_test_machine_receives_its_own_button(self):
        """_testMachine takes the button as an argument, never re-finds it.

        The old positional lookup counted siblings inside .machine-actions, but
        an active machine has no Activate button — so :nth-child(2) resolved to
        Test on inactive cards and Edit on active ones, and clicking Test
        relabelled Edit.
        """
        self.assertIn("async function _testMachine(id, btn)", self.machines)
        self.assertIn("_testMachine(m.id, testBtn)", self.machines)
        self.assertNotIn(".machine-action:nth-child(", self.machines)
        self.assertNotIn(".machine-card:nth-child(", self.machines)

    def test_test_machine_tunnel_messaging_keys_on_transport(self):
        """_testMachine's SSH-tunnel-specific messaging must key on
        transport_id, not the retired provider='ssh_proxy' value.

        Before this fix the branch checked machine.provider === 'ssh_proxy',
        a value no machine can ever have after the ssh-transport/backend
        split -- so a transport-routed machine's Test button silently fell
        through to the generic host:port messaging instead of reporting
        tunnel status ("SSH tunnel connected on port N" / "SSH tunnel:
        disconnected").
        """
        self.assertIn("machine.transport_id", self.machines)
        self.assertNotIn("machine.provider === 'ssh_proxy'", self.machines)

    def test_machine_test_toast_uses_fields_the_api_returns(self):
        """POST /api/machines/{id}/test returns {ok,status,error} only.

        Reading data.host/data.port off that response rendered the toast as
        "Connected to undefined:undefined".
        """
        self.assertNotIn("data.host", self.app)
        self.assertNotIn("data.port", self.app)
        self.assertNotIn("data.host", self.machines)
        self.assertNotIn("data.port", self.machines)

    def test_settings_save_button_scoped_to_tabs_it_writes(self):
        """The footer Save writes only the App tab's fields.

        Backends save through their own controls, and Skills and Usage are
        read-only, so the button is hidden there rather than silently
        reporting "No changes".
        """
        self.assertIn('id="settingsSave">Save<', self.html)
        self.assertNotIn("Save all", self.html)
        self.assertIn("save.hidden = tab !== 'app'", self.app)

    def test_settings_no_longer_carries_global_model_fields(self):
        """The default model is per-backend; the global inputs are gone.

        They were two free-text boxes describing whichever machine happened to
        be active, and fallback_model was never read by anything at all.
        """
        for removed in ("defaultModel", "fallbackModel"):
            self.assertNotIn(f'id="{removed}"', self.html)
            self.assertNotIn(f"byId('{removed}')", self.app)
        self.assertNotIn("fallback_model", self.app)

    def test_model_picker_offers_more_than_default_and_fallback(self):
        """The picker sources models from the backend, not two settings fields.

        With only default+fallback a turn could never be routed to a third
        model without changing the global default first. The list used to come
        from a hardcoded #modelSuggestions datalist; it now comes from
        GET /api/models, so the datalist is populated at runtime instead.
        """
        self.assertIn("_servedModels", self.app)
        self.assertIn('id="modelSuggestions"', self.html)

    def test_picker_does_not_harvest_models_from_transcripts(self):
        """A session's `model` records what an old chat used, not what is served.

        Pushing those ids into the picker turned a historical record into a
        menu of offers, so a model the current backend does not serve was
        selectable and every turn using it failed.
        """
        self.assertNotIn("_modelOptions.push(...sessions.map", self.app)

    def test_saving_settings_reloads_them(self):
        """saveSettings re-reads the server so the picker is not left stale."""
        save_body = self.app.split("async function saveSettings")[1].split(
            "async function saveChatDialog"
        )[0]
        self.assertIn("await loadSettings()", save_body)

    def test_live_history_polls_every_five_seconds(self):
        """A linked chat follows the terminal without the user asking."""
        self.assertIn("const SYNC_INTERVAL_MS = 5000", self.app)
        self.assertIn("setInterval", self.app)
        self.assertIn("/sync", self.app)

    def test_sync_is_scoped_and_guarded(self):
        """Only linked chats poll, and never over a turn in flight.

        Polling an unlinked chat is a request every five seconds that can
        never return anything; polling mid-stream would re-render the reply
        while it is still being written.
        """
        self.assertIn("if (!state.currentChat?.session_id) return;", self.app)
        self.assertIn("SYNC_BUSY_STATES", self.app)
        self.assertIn("stopTranscriptSync()", self.app)

    def test_refresh_button_exists_and_is_wired(self):
        self.assertIn('id="syncBtn"', self.html)
        self.assertIn("byId('syncBtn').addEventListener", self.app)
        # Hidden unless the open chat actually has a transcript behind it.
        self.assertIn("byId('syncBtn').hidden = !chat.session_id", self.app)

    def test_conversation_name_shows_in_the_strip(self):
        """The name used to be the topbar <h1>; it now lives in the strip.

        The directory used to be shown right after it (workspacePath), so the
        two would be read together -- removed at the user's request, since
        the workspace path is not something a reader of the strip needs.
        """
        self.assertIn('id="workspaceName"', self.html)
        self.assertNotIn('id="workspacePath"', self.html)
        self.assertIn("byId('workspaceName').textContent = chat.title", self.app)
        self.assertNotIn("workspacePath", self.app)

    def test_topbar_keeps_the_product_name(self):
        """It no longer swaps to the conversation title, which moved down."""
        self.assertNotIn("byId('topbarTitle').textContent = chat.title", self.app)

    def test_edit_and_refresh_sit_just_before_the_status(self):
        """Both move with the run state, not stranded beside the selects.

        .run-state carries margin-left:auto, so a button placed before it in
        source order would render at the far left of the strip. Grouping them
        is what keeps them adjacent to the status dot.
        """
        strip = self.html.split('id="workspaceStrip"')[1].split("</div>\n  <section")[0]
        for marker in ('id="editChatBtn"', 'id="syncBtn"', 'id="runState"'):
            self.assertIn(marker, strip)
        self.assertLess(strip.index('id="editChatBtn"'), strip.index('id="runState"'))
        self.assertLess(strip.index('id="syncBtn"'), strip.index('id="runState"'))
        self.assertIn("strip-right", self.html)
        self.assertIn(".strip-right{margin-left:auto", self.css)

    def test_strip_buttons_do_not_fatten_the_strip(self):
        """.btn-icon's 34px minimum would grow a 34px-tall strip."""
        self.assertIn(".strip-action{min-width:24px;min-height:24px", self.css)
        # Coarse pointers must still reach 44px, via the later media query.
        self.assertLess(
            self.css.index(".strip-action{"), self.css.index("@media(pointer:coarse)")
        )

    def test_dialog_checkboxes_are_not_stretched_to_full_width(self):
        """.dialog input sets width:100% for text fields; it caught these too.

        The checkbox filled its row with the glyph centred, pushing the radio
        and the model name off the right edge — so the per-model list showed a
        column of lone checkboxes, with no model names and no reachable
        default.
        """
        self.assertIn('.dialog input[type="checkbox"]', self.css)
        self.assertIn('.dialog input[type="radio"]', self.css)
        rule = self.css.split('.dialog input[type="checkbox"]')[1].split("}")[0]
        self.assertIn("width:auto", rule)

    def test_model_row_explains_both_controls(self):
        """The all-offered wording described only the tickbox.

        That is the state every backend starts in, so the radio column went
        unexplained exactly when a reader most needed it.
        """
        # The four tests below all check machines.js, not app.js: the
        # backend-cards/model-picker markup-building code moved there in a
        # later module split. The CSS checks are unaffected -- styles.css
        # was not split.
        self.assertIn("this backend\u2019s default for new chats", self.machines)
        self.assertIn("offered.title", self.machines)
        self.assertIn("isDefault.title", self.machines)

    def test_backend_state_is_worded_not_only_a_border(self):
        """Which backend is live was a 3px border — the panel's most important
        fact encoded as its least visible element."""
        self.assertIn("machine-state-live", self.machines)
        self.assertIn("'LIVE'", self.machines)
        self.assertIn("'STANDBY'", self.machines)
        self.assertIn(".machine-state-live{", self.css)

    def test_model_columns_are_labelled(self):
        """The checkbox and radio sat unlabelled; nothing said which was which."""
        self.assertIn("models-head", self.machines)
        for label in ("'Offered'", "'Default'", "'Model'"):
            self.assertIn(label, self.machines)
        self.assertIn(".models-head", self.css)

    def test_rail_terminates_on_the_default_row(self):
        """The signature of this layout: LIVE chip to default model, one path.

        The node hangs at a negative offset from its row, so the scroll
        container must be .models-body — clipping on .models-grid would cut it
        off exactly when a backend serves enough models to scroll.
        """
        self.assertIn("model-item-default", self.machines)
        self.assertIn(".machine-active .models-rail::before", self.css)
        self.assertIn(".machine-active .model-item-default::after", self.css)
        # Anchored to a line start: ".machine-models .models-body{" also
        # contains ".models-body{" and would match the wrong rule.
        body = self.css.split("\n.models-body{")[1].split("}")[0]
        self.assertIn("overflow-y:auto", body)
        grid = self.css.split("\n.models-grid{")[1].split("}")[0]
        self.assertNotIn("overflow", grid)

    def test_backend_name_is_not_truncated_by_its_endpoint(self):
        """The name shared a flex row with the endpoint and was cut to
        "Current AI...". They are now stacked in one identity block."""
        self.assertIn("machine-ident", self.machines)
        self.assertIn(".machine-ident{", self.css)

    def test_model_family_prefix_is_dimmed(self):
        """"azure_ai/" repeated down the column buries the part that differs."""
        self.assertIn("model-item-family", self.machines)
        self.assertIn(".model-item-family{", self.css)

    def test_messages_can_show_images(self):
        """The renderer emitted only text nodes and code, so an image named in
        a message was unreachable from the web UI by any route."""
        self.assertIn("export function openImageViewer", self.conversation)
        self.assertIn("image-chip", self.conversation)
        self.assertIn("pdf-chip", self.conversation)
        self.assertIn("openPdfViewer", self.conversation)
        self.assertIn("/file?path=", self.conversation)
        self.assertIn(".image-viewer{", self.css)
        self.assertIn(".pdf-viewer iframe", self.css)
        self.assertNotIn("buildComparisonPdf", self.html)
        self.assertNotIn("/api/reports/backend-model-comparison/build", self.app)
        self.assertNotIn("backend-model-comparison.download", self.html)
        self.assertNotIn("createComparisonPdfResource", self.conversation)

    def test_image_paths_resolve_against_the_open_chat(self):
        """A path means nothing without knowing whose workspace it is in."""
        self.assertIn("setImageContext", self.conversation)
        self.assertIn("setImageContext(chat.id)", self.conversation)

    def test_code_blocks_are_not_scanned_for_images(self):
        """A path inside a fence is being shown as text, not offered to open."""
        body = self.conversation.split("export function renderSafeText")[1]
        fenced = body.split("} else if (part) {")[0]
        self.assertIn("code.textContent", fenced)
        self.assertNotIn("renderProse", fenced)

    def test_viewer_can_be_dismissed(self):
        """A CSS tooltip cannot be closed; this is a dialog, so it must be."""
        self.assertIn("'Escape'", self.conversation)
        self.assertIn("aria-modal", self.conversation)

    def test_image_pattern_excludes_surrounding_punctuation(self):
        """A path written in prose as `shot.png` must not carry the backtick.

        The first version used \\S+, which matched the punctuation around a
        path too, so every filename quoted in a message was requested with a
        leading backtick attached and could never be found.
        """
        pattern = self.conversation.split("const IMAGE_REF =")[1].split(";")[0]
        self.assertNotIn("\\S+", pattern)
        self.assertIn("A-Za-z0-9._~-", pattern)

    def test_api_exports_contract(self):
        self.assertIn("export class ApiError", self.api)
        self.assertIn("export async function apiFetch", self.api)
        self.assertIn("export async function downloadMarkdown", self.api)

    def test_api_expired_session_redirects_to_login(self):
        self.assertIn("response.status === 401", self.api)
        self.assertIn("window.location.replace('/login')", self.api)
        self.assertIn("Session expired", self.api)
        self.assertIn("credentials: 'same-origin'", self.api)

    def test_chat_list_exports_contract(self):
        self.assertIn("export function filterChats", self.chat_list)
        self.assertIn("export function groupChats", self.chat_list)
        self.assertIn("export function createChatListController", self.chat_list)
        self.assertLess(self.chat_list.index("pinned:"), self.chat_list.index("recent:"))
        self.assertLess(self.chat_list.index("recent:"), self.chat_list.index("archived:"))

    def test_conversation_exports_contract(self):
        self.assertIn("export function parseTimestamp", self.conversation)
        self.assertIn("export function createConversationController", self.conversation)
        self.assertIn("export function renderSafeText", self.conversation)

    def test_application_owns_shared_state(self):
        self.assertIn("const state = {", self.app)
        self.assertIn("chats: []", self.app)
        self.assertIn("currentChat: null", self.app)
        self.assertIn("streamState: 'ready'", self.app)
        self.assertIn("DOMContentLoaded", self.app)

    def test_conversation_actions_are_accessible(self):
        for text in (
            "aria-haspopup", "aria-expanded", "role', 'menu'",
            "Rename", "Export", "Archive", "Delete", "Restore",
        ):
            self.assertIn(text, self.chat_list)

    def test_favouriting_is_a_row_control_not_a_menu_item(self):
        """Pin/Unpin left the ⋯ menu for a star on the row itself.

        It was the most-used action sitting behind the most clicks. The star
        still has to announce its state, or it is a toggle a screen reader
        cannot read.
        """
        self.assertIn("chat-favourite", self.chat_list)
        self.assertIn("aria-pressed", self.chat_list)
        self.assertIn("data-action", self.chat_list.replace("dataset.action", "data-action"))

    def test_archived_rows_stay_openable(self):
        """Archived conversations are marked, not disabled.

        Disabling the row made restoring a conversation the only way to read
        it -- mutating state just to look at something. The archived state is
        still visible through the class and the meta line.
        """
        self.assertNotIn("disabled = Boolean(chat.archived)", self.chat_list)
        self.assertIn("classList.add('archived')", self.chat_list)
        self.assertIn("metaParts.push('archived')", self.chat_list)

    def test_management_actions_and_restoration_exist(self):
        self.assertIn("downloadMarkdown(chat)", self.app)
        self.assertIn("method: 'DELETE'", self.app)
        self.assertIn("wc_last_chat", self.app)
        self.assertIn("wc_draft_", self.app)
        self.assertIn("Delete conversation", self.app)

    def test_stream_states_and_controls(self):
        for state in (
            "ready", "connecting", "thinking", "retrying",
            "responding", "stopped", "failed",
        ):
            self.assertIn(f"{state}:", self.conversation)
        self.assertIn("new AbortController()", self.conversation)
        self.assertIn("abortController.abort()", self.conversation)
        self.assertIn("streamCompleted = false", self.conversation)
        self.assertIn("Response stream ended before completion", self.conversation)
        self.assertIn("lastAttempt", self.conversation)
        self.assertIn("Jump to latest", self.html)
        self.assertIn("area.scrollHeight - area.scrollTop - area.clientHeight", self.conversation)

    def test_turn_progress_estimate_matches_the_supervisor_pattern(self):
        """Elapsed time until this chat has history, an estimated % after --
        same honest-fallback shape as the orchestrator pane's per-task version,
        never presented as a real measurement."""
        self.assertIn("_estimatedTurnPct", self.conversation)
        self.assertIn("_recordTurnDuration", self.conversation)
        self.assertIn("(est.)", self.conversation)
        # Clamped so it never claims a turn is 100% done before the server
        # has actually said so.
        self.assertIn("Math.min(99,", self.conversation)
        # Per chat id, not global -- a different conversation's history must
        # not leak into this one's estimate.
        self.assertIn("_turnDurationsByChat", self.conversation)
        # Guarded like the file's other module-scope timer, so a second call
        # to the factory cannot double it.
        self.assertIn("let _turnTicker = null;", self.conversation)
        self.assertIn("clearInterval(_turnTicker)", self.conversation)

    def test_queue_rows_are_tappable_and_the_list_is_capped(self):
        """The queue bar had three mobile complaints: a row's full prompt was
        readable only via desktop-only `title` hover, its Send/Discard buttons
        were too small to tap, and an unbounded list of rows could grow past
        the viewport. Same tap-to-reveal tooltip pattern as the auto-answer
        log, a capped scrollable list like `.lastcmd-menu`/`.auto-answer-menu`,
        and `.queue-btn` in the touch-target media query."""
        self.assertIn("_toggleQueueTooltip", self.conversation)
        self.assertIn("closeQueueTooltip", self.conversation)
        self.assertIn("text.tabIndex = 0;", self.conversation)
        self.assertIn("role", self.conversation)
        self.assertIn(".queue-list{", self.css)
        self.assertIn("max-height:min(40vh,220px);overflow-y:auto", self.css)
        self.assertIn(".queue-tooltip{", self.css)
        self.assertRegex(
            self.css,
            r"@media\(pointer:coarse\)\{[^}]*\.queue-btn",
        )

    def test_safe_rendering_and_mobile_css(self):
        self.assertIn("document.createTextNode", self.conversation)
        self.assertIn("code.textContent", self.conversation)
        self.assertIn("navigator.clipboard.writeText", self.conversation)
        self.assertIn("overflow-x:auto", self.css)
        self.assertIn("@media(pointer:coarse)", self.css)
        self.assertIn("@media(prefers-reduced-motion:reduce)", self.css)
        self.assertIn("focus-visible", self.css)
        self.assertIn("100dvh", self.css)

    def test_cli_sessions_render_contract(self):
        self.assertIn("setCliSessions", self.chat_list)
        self.assertIn("cliSessions = []", self.chat_list)
        self.assertIn("renderCli", self.chat_list)

    def test_cli_sessions_resume_action_contract(self):
        self.assertIn("resume-cli", self.chat_list)
        self.assertIn("onResumeCli", self.chat_list)
        self.assertIn("button.dataset.sessionId", self.chat_list)
        self.assertIn("resumeCliSession", self.app)
        self.assertIn("/api/sessions/", self.app)

    def test_cli_sessions_filtering_contract(self):
        self.assertIn("session.name", self.chat_list)
        self.assertIn("session.cwd", self.chat_list)
        self.assertIn("cliSessions.filter", self.chat_list)

    def test_cli_sessions_no_duplicate_webchat(self):
        self.assertIn("!item.webchat", self.app)

    def test_backends_tab_replaces_machines_and_models(self):
        """One tab: a model only means something against a backend.

        Two tabs let the model list describe whichever machine happened to be
        active, with nothing on screen saying so.
        """
        self.assertIn('data-tab="backends"', self.html)
        self.assertIn('id="panelBackends"', self.html)
        self.assertNotIn('data-tab="models"', self.html)
        self.assertNotIn('id="panelModels"', self.html)
        self.assertNotIn("panelModels", self.app)

    def test_models_render_inside_the_backend_that_serves_them(self):
        # _buildModelSection/machine-models moved to machines.js in a later
        # module split; loadModelsFor and the API call it makes stayed in
        # app.js, which machines.js imports it from.
        self.assertIn("_buildModelSection", self.machines)
        self.assertIn("machine-models", self.machines)
        self.assertIn("loadModelsFor", self.app)
        self.assertIn("machine_id=", self.app)

    def test_model_selection_saves_to_its_own_route(self):
        """PATCH /api/machines rejects the whole body on an unknown field, so
        the selection has its own endpoint."""
        self.assertIn("/models", self.app)
        self.assertIn("method: 'PUT'", self.app)

    def test_settings_has_no_stale_host_field(self):
        self.assertNotIn("settingsHost", self.app)

    def test_dialog_focus_trap_and_drawer_contract(self):
        self.assertIn("trapDialogFocus", self.app)
        self.assertIn("event.shiftKey", self.app)
        self.assertIn("aria-hidden", self.html)
        self.assertIn("aria-expanded", self.html)
        self.assertIn("inert>", self.html)
        self.assertIn("byId('sidebar').inert = false", self.app)
        self.assertIn("byId('sidebar').inert = true", self.app)
        self.assertIn(".chat-menu button,", self.css)
        self.assertIn("min-width:44px", self.css)
        self.assertIn("min-height:44px", self.css)

    # ── History section (past conversations read from transcripts) ──────────

    @staticmethod
    def _without_comments(source):
        """Strip // and /* */ comments so assertions see executable code only.

        The comments in chat-list.js discuss innerHTML at length precisely
        because it was removed, so a naive substring check matches the
        explanation rather than any real use.
        """
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        return "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("//")
        )

    def test_chat_list_never_uses_innerHTML(self):
        """Assigning markup into innerHTML was a stored XSS in search snippets.

        A message containing HTML was rendered as HTML, so anything a
        conversation had ever quoted could execute in the sidebar. Every node
        is built with textContent now; a structural assertion is the only
        guard the test harness can offer against it coming back.
        """
        code = self._without_comments(self.chat_list)
        # Guard the guard: if comment-stripping ever eats the whole file this
        # assertion would pass vacuously.
        self.assertIn("createElement", code)
        self.assertNotIn("innerHTML", code)

    def test_history_rows_offer_reading_the_transcript(self):
        self.assertIn("renderHistory", self.chat_list)
        self.assertIn("read-transcript", self.chat_list)

    def test_history_opens_the_viewer_through_an_event(self):
        """chat-list.js must not hold a handle on the viewer.

        The viewer mounts itself from transcript.js, so a direct reference
        would couple the two modules and break whichever loads second.
        """
        self.assertIn("wc:open-transcript", self.chat_list)
        self.assertIn("dispatchEvent", self.chat_list)

    def test_the_supervisor_section_links_to_the_supervisor(self):
        """The section names the orchestrator, so it should reach it.

        The only route in was a 📡 in the topbar, which does not say what it
        opens and sits nowhere near the agents it is about.
        """
        self.assertIn("open-orchestrator", self.chat_list)
        self.assertIn("onOpenSupervisor", self.chat_list)
        # openSupervisorPane's navigation moved to orchestrator.js in a later
        # module split.
        self.assertIn("orchestrator.html", self.orchestrator)

    def test_the_supervisor_heading_survives_an_empty_queue(self):
        """The link has to be reachable when nothing is waiting -- that is
        exactly when you want to go and look at the orchestrator."""
        self.assertIn("nothingToShow", self.chat_list)
        body = self.chat_list.split("function renderSupervisor")[1].split("function render(")[0]
        self.assertIn("list.appendChild(heading)", body)
        # The early return must come after the heading is appended, not before.
        self.assertLess(body.index("list.appendChild(heading)"),
                        body.index("if (nothingToShow) return;"))

    def test_history_is_populated_through_set_history(self):
        self.assertIn("setHistory", self.chat_list)

    def test_history_does_not_repeat_sessions_already_listed(self):
        """A live CLI session appears in its own section; showing it again
        under History listed the same conversation twice."""
        self.assertIn("alreadyShown", self.chat_list)

    def test_degraded_chats_show_a_badge(self):
        """chat.degraded rides along on GET /api/chats once Task 1 lands;
        the sidebar must read it rather than silently ignoring the field."""
        self.assertIn("chat.degraded", self.chat_list)
        self.assertIn("chat-degraded", self.chat_list)
        self.assertIn(".chat-degraded{", self.css)

    def test_degraded_supervisors_are_marked_on_their_status_badge(self):
        self.assertIn("s.degraded", self.supervisor_list_js)
        self.assertIn("degraded_reason", self.supervisor_list_js)

    def test_rail_is_rendered_and_wired_into_the_pane(self):
        self.assertIn("export function renderRail(tasks)", self.rail)
        self.assertIn('id="task-rail"', self.supervisor_html)
        self.assertIn("renderRail(state.tasks)", self.tasks_js)
        self.assertIn("taskRail", self.dom_js)

    def test_human_gate_marker_exists_and_is_wired(self):
        self.assertIn("export function updateGateMarker(supervisorStatus, members)", self.banners_js)
        self.assertIn('id="topbar-gate"', self.supervisor_html)
        self.assertIn("updateGateMarker(", self.members_js)
        self.assertIn("updateGateMarker(", self.stream_js)
        self.assertIn("topbarGate", self.dom_js)
        self.assertIn("state.members", self.members_js)
        # Members must load when a orchestrator is opened, not only after using
        # the add/remove picker -- otherwise the gate marker is blind until
        # someone happens to touch that dialog.
        self.assertIn("loadMembers(", self.list_js)
        self.assertIn("loadMembers", self.main_js_supervisor)

    def test_human_gate_marker_toggles_the_visible_class(self):
        """`#topbar .notif-badge` starts at opacity:0/pointer-events:none and
        only `.visible` turns it on -- see updateNotificationBadge, the
        sibling function that already does this correctly for #topbar-badge.
        Toggling only `hidden` (without `.visible`) leaves the marker
        invisible even when un-hidden."""
        body = self.banners_js.split(
            "export function updateGateMarker(supervisorStatus, members)"
        )[1].split("\n\n")[0]
        self.assertIn('classList.add("visible")', body)
        self.assertIn('classList.remove("visible")', body)

    def test_gate_badge_overrides_pointer_events_back_to_auto(self):
        """.gate-badge inherits `#topbar .notif-badge { pointer-events: none }`
        -- without an override the marker would be visible but unclickable."""
        gate_rule = self.supervisor_html.split(".gate-badge {")[1].split("}")[0]
        self.assertIn("pointer-events: auto", gate_rule)

    def test_esc_escapes_both_quote_characters(self):
        """degraded_reason is interpolated into a `title="..."` HTML attribute
        inside a template string assigned via innerHTML (web/assets/orchestrator
        /list.js). That is only safe because esc() also escapes quotes, not
        just angle brackets -- pin it so an unrelated future edit to esc()
        cannot silently reopen that attribute-breakout."""
        body = self.main_js_supervisor.split("export function esc(str)")[1].split("\n\n")[0]
        self.assertIn('.replace(/"/g,', body)
        self.assertIn(".replace(/'/g,", body)


if __name__ == "__main__":
    unittest.main()
