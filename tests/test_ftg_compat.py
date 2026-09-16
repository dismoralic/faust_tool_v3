import unittest

from userbot.core import ftg_compat as loader


class FTGCompatibilityTests(unittest.TestCase):
    def test_hikka_tag_metadata_is_preserved(self):
        @loader.watcher()
        @loader.tag("no_commands", out=True)
        async def sample(message):
            return message

        self.assertTrue(sample.ftg_watcher)
        self.assertTrue(sample.ftg_tags["no_commands"])
        self.assertTrue(sample.ftg_tags["out"])

    def test_module_config_validation_and_defaults(self):
        config = loader.ModuleConfig(
            loader.ConfigValue(
                "limit",
                3,
                "Лимит",
                validator=loader.validators.Integer(minimum=1, maximum=5),
            )
        )
        config.set("limit", "5")
        self.assertEqual(config["limit"], 5)
        self.assertEqual(config.getdef("limit"), 3)
        with self.assertRaises(loader.validators.ValidationError):
            config.set("limit", 8)

    def test_legacy_module_config_triples(self):
        config = loader.ModuleConfig("greeting", "hello", lambda: "Greeting")
        self.assertEqual(config["greeting"], "hello")
        self.assertEqual(config.getdoc("greeting"), "Greeting")

    def test_hidden_validator_delegates(self):
        validator = loader.validators.Hidden(loader.validators.Integer(minimum=1))
        self.assertEqual(validator("2"), 2)


if __name__ == "__main__":
    unittest.main()
