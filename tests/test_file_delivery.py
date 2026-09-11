import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import file_delivery


def telegram_user(username="admin"):
    return SimpleNamespace(id=100, username=username)


class FilePathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.admin = telegram_user()

    def tearDown(self):
        self.temporary.cleanup()

    def resolve_as_admin(self, requested):
        with (
            patch.object(file_delivery, "PROJECT_ROOT", self.root),
            patch.object(file_delivery, "is_admin", return_value=True),
            patch.object(file_delivery, "is_super_admin", return_value=False),
        ):
            return file_delivery.resolve_file_for_user(requested, self.admin)

    def test_admin_can_retrieve_an_ordinary_hidden_project_file(self):
        hidden = self.root / ".gitignore"
        hidden.write_text("uploads/\n", encoding="utf-8")

        self.assertEqual(self.resolve_as_admin(".gitignore"), hidden)
        self.assertEqual(self.resolve_as_admin(str(hidden)), hidden)

    def test_admin_cannot_retrieve_environment_or_private_key_files(self):
        for name in (".env", ".env.production", "server.pem", "id_rsa"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text("secret", encoding="utf-8")
                with self.assertRaisesRegex(
                    file_delivery.FileAccessError,
                    "protected credentials",
                ):
                    self.resolve_as_admin(name)

    def test_admin_cannot_escape_project_through_absolute_or_symlink_path(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "linked.txt"
        link.symlink_to(outside)
        try:
            for requested in (str(outside), "linked.txt"):
                with self.subTest(requested=requested), self.assertRaisesRegex(
                    file_delivery.FileAccessError,
                    "only from the bot project",
                ):
                    self.resolve_as_admin(requested)
        finally:
            outside.unlink(missing_ok=True)

    def test_super_admin_can_retrieve_a_readable_file_outside_project(self):
        outside = self.root.parent / "owner-file.txt"
        outside.write_text("owner data", encoding="utf-8")
        try:
            with (
                patch.object(file_delivery, "PROJECT_ROOT", self.root),
                patch.object(file_delivery, "is_admin", return_value=True),
                patch.object(
                    file_delivery,
                    "is_super_admin",
                    return_value=True,
                ),
            ):
                resolved = file_delivery.resolve_file_for_user(
                    str(outside),
                    telegram_user("owner"),
                )
            self.assertEqual(resolved, outside)
        finally:
            outside.unlink(missing_ok=True)


class GetFileCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_get_uploads_the_resolved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "report.txt")
            path.write_text("report", encoding="utf-8")
            status = SimpleNamespace(edit_text=AsyncMock())
            user = telegram_user()
            message = SimpleNamespace(
                from_user=user,
                reply_text=AsyncMock(return_value=status),
                reply_document=AsyncMock(),
            )
            update = SimpleNamespace(message=message)
            context = SimpleNamespace(args=[str(path)])

            with (
                patch.object(file_delivery, "is_admin", return_value=True),
                patch.object(
                    file_delivery,
                    "resolve_file_for_user",
                    return_value=path,
                ),
            ):
                await file_delivery.get_file(update, context)

        message.reply_document.assert_awaited_once()
        self.assertEqual(
            message.reply_document.await_args.kwargs["caption"],
            "report.txt",
        )
        status.edit_text.assert_awaited_once_with("✅ Uploaded report.txt.")

    async def test_non_admin_is_rejected_before_path_resolution(self):
        message = SimpleNamespace(
            from_user=telegram_user("user"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(args=["README.md"])

        with (
            patch.object(file_delivery, "is_admin", return_value=False),
            patch.object(file_delivery, "resolve_file_for_user") as resolve,
        ):
            await file_delivery.get_file(update, context)

        resolve.assert_not_called()
        message.reply_text.assert_awaited_once_with(
            "❌ Only admins can retrieve files."
        )


if __name__ == "__main__":
    unittest.main()
