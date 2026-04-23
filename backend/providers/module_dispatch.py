from importlib import import_module
from typing import Any, Iterable, Optional


class UnavailableBackendAttribute:
    def __init__(self, module_name: str, attribute_name: str):
        self._module_name = module_name
        self._attribute_name = attribute_name

    def _raise(self) -> None:
        raise NotImplementedError(
            f"{self._module_name}.{self._attribute_name} is not implemented for the active backend mode"
        )

    def __call__(self, *args, **kwargs):
        self._raise()

    def __getattr__(self, item: str) -> Any:
        self._raise()

    def __bool__(self) -> bool:
        self._raise()

    def __iter__(self):
        self._raise()

    def __repr__(self) -> str:
        return f"<UnavailableBackendAttribute {self._module_name}.{self._attribute_name}>"


def load_backend_module(
    module_globals: dict,
    firebase_module_name: str,
    supabase_module_name: Optional[str] = None,
    exported_names: Optional[Iterable[str]] = None,
) -> None:
    module_name = module_globals["__name__"]
    module_names: list[str] = [firebase_module_name]
    if supabase_module_name:
        module_names.append(supabase_module_name)

    for implementation_name in module_names:
        implementation = import_module(implementation_name)
        public_names = [name for name in dir(implementation) if not name.startswith("__")]
        for name in public_names:
            module_globals[name] = getattr(implementation, name)
    if exported_names is not None:
        module_globals["__all__"] = list(exported_names)
    elif hasattr(implementation, "__all__"):
        module_globals["__all__"] = list(getattr(implementation, "__all__"))

    def __getattr__(attribute_name: str) -> Any:
        if attribute_name.startswith("__"):
            raise AttributeError(attribute_name)
        return UnavailableBackendAttribute(module_name, attribute_name)

    module_globals["__getattr__"] = __getattr__
    if exported_names is not None:
        module_globals["__all__"] = list(exported_names)
