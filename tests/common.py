
import os
import sys
sys.path.insert(0, '../')


if sys.version_info < (2, 7) or (0x03000000 <= sys.hexversion < 0x03010000):
    # py26 or py30
    import unittest2 as unittest
else:
    import unittest

import flubber


class dummy(object):
    pass


class FlubberTestCase(unittest.TestCase):

    def setUp(self):
        self.loop = flubber.EventLoop()

    def tearDown(self):
        self.loop.destroy()
        self.loop = None

