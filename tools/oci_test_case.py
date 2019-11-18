import logging
from unittest import TestCase

from tree_config import TreeConfigParser


class OciTestCase(TestCase):
    """Base class for OCI util test case
    """
    test_config_dir = None

    @classmethod
    def _set_base(cls, namespace):
        """Set test base directory.
        from the base we derived the path to test configuration

        Parameters
        ----------
        namespace : string
            the tests nase dir

        """
        OciTestCase.test_config_dir = namespace

    def setUp(self):
        """Test setUp method
            NOTE : any subclass implementation must call it during their own
            setUp phase
        """
        self.properties = TreeConfigParser(OciTestCase.test_config_dir, self)
        self.logger = logging.getLogger('oci-utils.%s' % self.__class__.__name__)
