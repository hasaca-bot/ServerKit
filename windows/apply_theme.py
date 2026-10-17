"""Install a theme.json into the panel and make it the panel-wide default.

Run with the panel's own interpreter from the backend directory:

    cd /opt/serverkit/backend && ../venv/bin/python /path/apply_theme.py theme.json

It goes through the same theme_service the Settings -> Appearance page uses,
so the token whitelist and value validators still apply and the theme is an
ordinary installed theme afterwards (it survives updates and can be removed
from the panel).
"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())

# The panel's settings live in the install's .env, not in the process
# environment; load it before the app is imported, as backend/cli.py does.
from dotenv import load_dotenv  # noqa: E402

for _env in (os.path.join(os.getcwd(), '.env'), os.path.join(os.getcwd(), '..', '.env')):
    if os.path.exists(_env):
        load_dotenv(_env)
        break

from app import create_app  # noqa: E402
from app.services import theme_service  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print('usage: apply_theme.py <theme.json>', file=sys.stderr)
        return 2
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        raw = json.load(f)

    app = create_app()
    with app.app_context():
        theme, err = theme_service.import_theme(raw, source='import')
        if err:
            print(f'Theme rejected: {err}', file=sys.stderr)
            return 1
        ok, err = theme_service.set_default_slug(theme['slug'])
        if not ok:
            print(f'Could not set the default theme: {err}', file=sys.stderr)
            return 1
        print(f"Theme '{theme['name']}' installed and set as the panel default.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
