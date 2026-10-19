import os
import shutil
import sys


def get_profile_folder():
    if sys.platform == 'win32':
        return os.path.join(os.getenv('APPDATA'), 'dolphin_anty', 'browser_profiles')
    elif sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/dolphin_anty/browser_profiles')
    else:
        return os.path.expanduser('~/.config/dolphin_anty/browser_profiles')


def clean_if_exists(dir_to_clean):
    if os.path.exists(dir_to_clean):
        try:
            shutil.rmtree(dir_to_clean)
        except OSError:
            pass


def _profiles_page_data(response):
    """Return profile list from Dolphin API page response, or None if unusable."""
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if data is None:
        return None
    if not isinstance(data, list):
        return None
    return data


def collect_garbage(api=None, profile_id=None):
    profile_folder = get_profile_folder()

    if api is not None:
        api_profiles = []
        page = 1
        while True:
            try:
                response = api.get_profiles(page=page)
            except Exception:
                api_profiles = None
                break
            profiles = _profiles_page_data(response)
            if profiles is None:
                api_profiles = None
                break
            if not profiles:
                break
            api_profiles.extend(profiles)
            page += 1

        if not api_profiles:
            return

        api_profile_ids = [f'{profile["id"]}' for profile in api_profiles]

        if not os.path.isdir(profile_folder):
            return

        local_profile_ids = os.listdir(profile_folder)

        garbage_profile_ids = set(local_profile_ids) - set(api_profile_ids)

        for profile_id in garbage_profile_ids:
            profile_path = os.path.join(profile_folder, profile_id)
            clean_if_exists(profile_path)
    else:
        profile_path = os.path.join(profile_folder, f'{profile_id}')
        clean_if_exists(profile_path)
