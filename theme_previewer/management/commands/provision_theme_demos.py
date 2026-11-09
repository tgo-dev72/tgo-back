from django.core.management.base import BaseCommand, CommandError

from api.designs import list_designs
from theme_previewer.provisioning import provision_theme_demo


class Command(BaseCommand):
    help = "Idempotently provision official, read-only demonstration tenants from the theme registry."

    def add_arguments(self, parser):
        parser.add_argument("--theme", action="append", dest="themes", help="Theme key; repeat to provision several. Default: every ready theme.")
        parser.add_argument("--public-url-template", default="", help="HTTPS template containing {theme}, e.g. https://demo-{theme}.tijarago.com")
        parser.add_argument("--enable", action="store_true", help="Enable only after the public URLs have been deployed and validated.")

    def handle(self, *args, **options):
        ready = {item["key"] for item in list_designs() if item.get("status") == "ready"}
        themes = options["themes"] or sorted(ready)
        unknown = set(themes) - ready
        if unknown:
            raise CommandError(f"Unknown or unavailable themes: {', '.join(sorted(unknown))}")
        if options["enable"] and "{theme}" not in options["public_url_template"]:
            raise CommandError("--enable requires --public-url-template containing {theme}.")
        for theme in themes:
            url = options["public_url_template"].format(theme=theme) if options["public_url_template"] else ""
            try:
                profile = provision_theme_demo(theme, public_url=url, enable=options["enable"])
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"{theme}: {profile.store.slug} [{profile.deployment_status}]")
        self.stdout.write(self.style.SUCCESS(f"Provisioned {len(themes)} theme demo tenant(s)."))
