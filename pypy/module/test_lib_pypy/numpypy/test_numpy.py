class AppTestNumpy:
    spaceconfig = dict(usemodules=['micronumpy'])

    def setup_class(cls):
        import py
        py.test.skip('the applevel parts are not ready for py3k')

    def test_imports(self):
        try:
            import numpy   # fails if 'numpypy' was not imported so far
        except ImportError:
            pass
        import numpypy
        import numpy     # works after 'numpypy' has been imported

    def test_min_max_after_import(self):
        from numpypy import *
        assert min(1, 100) == 1
        assert min(100, 1) == 1

        assert max(1, 100) == 100
        assert max(100, 1) == 100
