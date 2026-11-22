import os
import unittest
from unittest import mock

import youtube_uploader as yu


class DistroKidReferralTest(unittest.TestCase):
    def _desc(self, **env):
        up = yu.YouTubeUploader.__new__(yu.YouTubeUploader)
        up.uploads_today = 0
        with mock.patch.dict(os.environ, env, clear=False):
            return up._generate_metadata({'track_name': 'Beyond The Horizon'})['description']

    def test_referral_is_labeled_affiliate(self):
        d = self._desc()
        self.assertIn('https://distrokid.com/vip/seven/3497468', d)
        self.assertIn('Affiliate link', d)
        self.assertLess(len(d), 5000)

    def test_referral_can_be_disabled(self):
        self.assertNotIn('distrokid.com', self._desc(DISTROKID_REFERRAL_URL=''))


if __name__ == '__main__':
    unittest.main()
