"""Makes `tests` a package so modules can share `factories`/`conftest` by
relative import — and so `tests.factories` can never shadow a real module on
sys.path."""
