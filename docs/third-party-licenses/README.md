# Bundled dependencies

The release includes copies of the license files shipped with the installed Python, PyQt6/Qt, HTTP and supporting packages used for the Windows build. Each dependency retains its own license; the repository's original MIT license is preserved for LastSync's source.

PyQt6 is distributed under GPLv3 or a separate Riverbank commercial license. Consult the included upstream license text for redistribution terms. Providing an executable does not replace those terms. The full LastSync source and build instructions accompany the portable release.

The optional Mesa software OpenGL renderer is omitted from the executable because this application uses QtWidgets raster painting, not QOpenGLWidget or Qt Quick.
