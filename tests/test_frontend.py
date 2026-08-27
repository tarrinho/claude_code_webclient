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
        self.assertIn('type="module" src="/assets/app.js"', self.html)
        self.assertIn('rel="stylesheet" href="/assets/styles.css"', self.html)
        self.assertNotIn("<style>", self.html)
        self.assertNotIn("async function sendMessage", self.html)
        self.assertNotIn("<script>\n", self.html)

    def test_api_exports_contract(self):
        self.assertIn("export class ApiError", self.api)
        self.assertIn("export async function apiFetch", self.api)
        self.assertIn("export async function downloadMarkdown", self.api)

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
        self.assertIn("disabled = Boolean(chat.archived)", self.chat_list)

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

    def test_dialog_focus_trap_and_drawer_contract(self):
        self.assertIn("trapDialogFocus", self.app)
        self.assertIn("event.shiftKey", self.app)
        self.assertIn("aria-hidden", self.html)
        self.assertIn("aria-expanded", self.html)


if __name__ == "__main__":
    unittest.main()
