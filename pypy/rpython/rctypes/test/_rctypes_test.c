/* simple test, currently only for structures */
#include <Python.h>
#ifdef MS_WIN32
#include <windows.h>
#endif
#if defined(MS_WIN32) || defined(__CYGWIN__)
#define EXPORT(x) __declspec(dllexport) x
#else
#define EXPORT(x) x
#endif

PyMethodDef module_methods[] = {
	{ NULL, NULL, 0, NULL},
};


typedef struct tagpoint {
	int x;
	int y;
} point;

EXPORT(int) _testfunc_byval(point in, point *pout)
{
	if (pout) {
		pout->x = in.x;
		pout->y = in.y;
	}
	return in.x + in.y;
}

DL_EXPORT(void)
init_rctypes_test(void)
{
	Py_InitModule("_rctypes_test", module_methods);
}

