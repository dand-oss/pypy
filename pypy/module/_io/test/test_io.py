from pypy.conftest import gettestobjspace

class AppTestIoModule:
    def setup_class(cls):
        cls.space = gettestobjspace(usemodules=['_io'])

    def test_import(self):
        import io

    def test_iobase(self):
        import io
        io.IOBase()

        class MyFile(io.BufferedIOBase):
            def __init__(self, filename):
                pass
        MyFile("file")

    def test_openclose(self):
        import io
        with io.BufferedIOBase() as f:
            assert not f.closed
        assert f.closed

    def test_iter(self):
        import io
        class MyFile(io.IOBase):
            def __init__(self):
                self.lineno = 0
            def readline(self):
                self.lineno += 1
                if self.lineno == 1:
                    return "line1"
                elif self.lineno == 2:
                    return "line2"
                return ""

        assert list(MyFile()) == ["line1", "line2"]
