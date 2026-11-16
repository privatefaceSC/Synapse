"""Restart the AlwaysData WSGI site and confirm its loaded commit."""

import base64
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ACCOUNT = 'synapseivan'
SITE_ID = 1075475
PING_URL = 'https://synapseivan.alwaysdata.net/api/ping'


def main():
    api_key = os.environ['ALWAYSDATA_API_KEY']
    expected = os.environ['EXPECTED_SHA'][:12]
    credential = base64.b64encode(
        f'{api_key} account={ACCOUNT}:'.encode('utf-8')).decode('ascii')
    request = Request(
        f'https://api.alwaysdata.com/v1/site/{SITE_ID}/restart/',
        data=b'',
        method='POST',
        headers={'Authorization': f'Basic {credential}'},
    )
    with urlopen(request, timeout=20) as response:
        if response.status not in (200, 201, 202, 204):
            raise RuntimeError(f'AlwaysData restart returned HTTP {response.status}')
    print('Site restart requested')

    deadline = time.monotonic() + 120
    last_result = 'no response'
    while time.monotonic() < deadline:
        try:
            with urlopen(PING_URL, timeout=10) as response:
                payload = json.load(response)
            revision = payload.get('revision')
            last_result = f'revision {revision}'
            if response.status == 200 and payload.get('ok') is True and revision == expected:
                print(f'Deployed and verified revision {expected}')
                return
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_result = type(exc).__name__
        time.sleep(3)

    raise RuntimeError(
        f'Site did not load revision {expected} within 120 seconds ({last_result})')


if __name__ == '__main__':
    main()
