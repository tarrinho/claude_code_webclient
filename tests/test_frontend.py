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

    def test_html_uses_external_assets(self):
        # Trailing quote omitted so a cache-busting ?v=N query stays valid.
        self.assertIn('type="module" src="/assets/app.js', self.html)
        self.assertIn('rel="stylesheet" href="/assets/styles.css"', self.html)
        self.assertNotIn("<style>", self.html)
        self.assertNotIn("async function sendMessage", self.html)
        self.assertNotIn("<script>\n", self.html)

    def test_machine_form_script_matches_markup(self):
        """app.js must not read machine inputs that index.html no longer defines.

        Sending fields the form does not collect makes the server reject the
        whole PATCH body, which is what broke machine editing in 0.3.1.
        """
        for element_id in ("machinePort", "machineBaseUrl", "machineDescription"):
            self.assertNotIn(element_id, self.html)
            self.assertNotIn(element_id, self.app)

    def test_test_machine_receives_its_own_button(self):
        """_testMachine takes the button as an argument, never re-finds it.

        The old positional lookup counted siblings inside .machine-actions, but
        an active machine has no Activate button — so :nth-child(2) resolved to
        Test on inactive cards and Edit on active ones, and clicking Test
        relabelled Edit.
        """
        self.assertIn("async function _testMachine(id, btn)", self.app)
        self.assertIn("_testMachine(m.id, testBtn)", self.app)
        self.assertNotIn(".machine-action:nth-child(", self.app)
        self.assertNotIn(".machine-card:nth-child(", self.app)

    def test_machine_test_toast_uses_fields_the_api_returns(self):
        """POST /api/machines/{id}/test returns {ok,status,error} only.

        Reading data.host/data.port off that response rendered the toast as
        "Connected to undefined:undefined".
        """
        self.assertNotIn("data.host", self.app)
        self.assertNotIn("data.port", self.app)

    def test_settings_save_button_scoped_to_tabs_it_writes(self):
        """The footer Save writes only Models and App fields.

        Machines save through their own form and Skills is read-only, so the
        button is hidden there rather than silently reporting "No changes".
        """
        self.assertIn('id="settingsSave">Save<', self.html)
        self.assertNotIn("Save all", self.html)
        self.assertIn("save.hidden = tab === 'machines' || tab === 'skills'", self.app)

    def test_model_fields_can_be_cleared(self):
        """Saving compares against the loaded value, not truthiness.

        `if (defaultModel) body.default_model = ...` skipped empty strings, so
        blanking a model field and saving reported "No changes to save" and the
        old value stayed. The API accepts "" and clears the setting.
        """
        self.assertNotIn("if (defaultModel) body.default_model", self.app)
        self.assertNotIn("if (fallbackModel) body.fallback_model", self.app)
        self.assertIn("_loadedSettings.default_model", self.app)
        self.assertIn("_loadedSettings.fallback_model", self.app)

    def test_model_picker_includes_datalist_presets(self):
        """The picker reads #modelSuggestions rather than default+fallback only.

        With only two entries a turn could never be routed to a third model
        without changing the global default first.
        """
        self.assertIn("#modelSuggestions option", self.app)
        self.assertIn('id="modelSuggestions"', self.html)

    def test_saving_settings_reloads_them(self):
        """saveSettings re-reads the server so the picker is not left stale."""
        save_body = self.app.split("async function saveSettings")[1].split(
            "async function saveChatDialog"
        )[0]
        self.assertIn("await loadSettings()", save_body)

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
            "Unpin", "Rename", "Export", "Archive", "Delete", "Restore",
        ):
            self.assertIn(text, self.chat_list)

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

    def test_model_settings_fields_are_wired(self):
        for field in ("defaultModel", "fallbackModel"):
            self.assertIn(f"id=\"{field}\"", self.html)
            self.assertIn(f"byId('{field}')", self.app)
        self.assertNotIn("settingsHost", self.app)

    def test_model_settings_save_contract(self):
        self.assertIn("default_model", self.app)
        self.assertIn("fallback_model", self.app)
        self.assertIn("data.default_model", self.app)
        self.assertIn("data.fallback_model", self.app)

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


if __name__ == "__main__":
    unittest.main()
