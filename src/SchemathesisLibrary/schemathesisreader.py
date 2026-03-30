# Copyright 2025-     Tatu Aalto
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from DataDriver.AbstractReaderClass import AbstractReaderClass  # type: ignore
from DataDriver.ReaderConfig import TestCaseData  # type: ignore
from hypothesis import HealthCheck, Phase, Verbosity, given, settings
from hypothesis import strategies as st
from robot.api import logger
from robot.utils.importer import Importer  # type: ignore
from schemathesis import Case, GenerationMode, openapi
from schemathesis.config import SchemathesisConfig
from schemathesis.core import NotSet
from schemathesis.core.result import Ok


@dataclass
class Options:
    max_examples: int
    headers: dict[str, Any] | None = None
    path: "Path|None" = None
    url: str | None = None
    auth: str | None = None
    hook: str | None = None


class SchemathesisReader(AbstractReaderClass):
    options: "Options|None" = None

    def get_data_from_source(self) -> list[TestCaseData]:
        if not self.options:
            raise ValueError("Options must be set before calling get_data_from_source.")
        url = self.options.url
        path = self.options.path
        if path and not Path(path).is_file():
            raise ValueError(f"Provided path '{path}' is not a valid file.")
        config, generation_mode = self._load_config()
        if path:
            schema = openapi.from_path(path, config=config)
        elif url:
            headers = self.options.headers or {}
            schema = openapi.from_url(url, headers=headers, config=config)
        else:
            raise ValueError("Either 'url' or 'path' must be provided to SchemathesisLibrary.")
        all_cases: list[TestCaseData] = []
        if self.options.auth:
            import_extensions(self.options.auth)
            logger.info(f"Using auth extension from: {self.options.auth}")
        self._import_hooks()
        for op in schema.get_all_operations():
            if isinstance(op, Ok):
                strategy = op.ok().as_strategy(generation_mode=generation_mode).map(from_case)  # type: ignore
                add_examples(strategy, all_cases, self.options.max_examples)  # type: ignore
        return all_cases

    def _load_config(self) -> tuple[SchemathesisConfig, GenerationMode]:
        config = SchemathesisConfig.discover()
        generation_mode = GenerationMode.POSITIVE
        if self.options is None:
            return config, generation_mode
        if config.config_path and config.projects.default.generation:
            logger.info(f"Config file path: {config.config_path}")
            if config.projects.default.generation.max_examples is not None:
                self.options.max_examples = config.projects.default.generation.max_examples
                logger.info(f"Using max_examples from config: {self.options.max_examples}")
            if modes := config.projects.default.generation.modes:
                generation_mode = modes[0]
                logger.info(f"Using first generation mode from config: {generation_mode}")
        else:
            logger.info("No schemathesis.toml config file found, using defaults")
        return config, generation_mode

    def _import_hooks(self) -> None:
        if not self.options:
            return
        if not self.options.hook:
            return
        for hook in self.options.hook.split(";"):
            logger.info(f"Using hook extension from: {hook}")
            import_extensions(hook)


def from_case(case: Case) -> TestCaseData:
    """
    Generate descriptive test case name from Schemathesis Case object.

    Creates human-readable test names based on:
    - Query parameters (accountIds, actionGroups, orgId, etc.)
    - Request body characteristics
    - Special cases (empty values, long strings, special characters)
    - Hook modifications (missing required parameters)
    """
    # Start with the operation label
    test_name = case.operation.label
    descriptors = []

    # Check if hook has marked parameters as missing
    hook_modifications = []
    if hasattr(case, '_modified_by_hook'):
        hook_modifications = case._modified_by_hook

    # Analyze query parameters
    if case.query or hook_modifications:
        query_desc = []

        # Add missing parameters first (marked by hook)
        for modification in hook_modifications:
            query_desc.append(modification)

        # Then add present parameters
        if case.query:
            for key, value in case.query.items():
                # Handle empty/null values
                if value == "" or value is None:
                    query_desc.append(f"{key}=EMPTY")
                # Handle very long values
                elif isinstance(value, str) and len(value) > 30:
                    query_desc.append(f"{key}=LONG[{len(value)}]")
                # Handle special characters
                elif isinstance(value, str) and any(char in value for char in "!@#$%^&*()[]{}"):
                    query_desc.append(f"{key}=SPECIAL")
                # Handle numeric strings
                elif isinstance(value, str) and value.isdigit():
                    query_desc.append(f"{key}=NUM[{value[:10]}]")
                # Handle UUID format (36 chars with 4 hyphens)
                elif isinstance(value, str) and len(value) == 36 and value.count('-') == 4:
                    query_desc.append(f"{key}=UUID[{value[:8]}]")
                # Handle whitespace
                elif isinstance(value, str) and (value.startswith(" ") or value.endswith(" ")):
                    query_desc.append(f"{key}=SPACE")
                # Normal values - truncate if needed
                else:
                    str_val = str(value)
                    if len(str_val) > 20:
                        query_desc.append(f"{key}={str_val[:17]}...")
                    else:
                        query_desc.append(f"{key}={str_val}")

        if query_desc:
            # Join all query parameters
            descriptors.append(f"[{', '.join(query_desc)}]")

    # Analyze path parameters
    if case.path_parameters:
        path_desc = []
        for key, value in case.path_parameters.items():
            str_val = str(value)[:15]
            path_desc.append(f"{key}={str_val}")
        if path_desc:
            descriptors.append(f"Path[{', '.join(path_desc)}]")

    # Analyze request body
    if case.body and not isinstance(case.body, NotSet):
        if isinstance(case.body, dict):
            body_keys = list(case.body.keys())
            if len(body_keys) <= 3:
                descriptors.append(f"Body[{', '.join(body_keys)}]")
            else:
                descriptors.append(f"Body[{len(body_keys)}fields]")
        elif isinstance(case.body, list):
            descriptors.append(f"Body[list:{len(case.body)}]")
        elif isinstance(case.body, str):
            if len(case.body) == 0:
                descriptors.append("Body[EMPTY]")
            elif len(case.body) > 100:
                descriptors.append(f"Body[{len(case.body)}bytes]")
            else:
                descriptors.append(f"Body[str]")

    # Build final test name
    if descriptors:
        test_name = f"{test_name} {' '.join(descriptors)}"
    else:
        # Fallback to ID if no meaningful descriptors
        test_name = f"{test_name} - {case.id}"

    # Ensure name isn't too long (Robot Framework limit ~255 chars)
    max_length = 180
    if len(test_name) > max_length:
        test_name = test_name[:max_length-3] + "..."

    return TestCaseData(
        test_case_name=test_name,
        arguments={"${case}": case},
    )


def add_examples(strategy: st.SearchStrategy, container: list[TestCaseData], max_examples: int) -> None:
    @given(strategy)
    @settings(
        database=None,
        max_examples=max_examples,
        deadline=None,
        verbosity=Verbosity.quiet,
        phases=(Phase.generate,),
        suppress_health_check=list(HealthCheck),
    )
    def example_generating_inner_function(ex: Any) -> None:
        container.append(ex)

    example_generating_inner_function()


def import_extensions(library: str | Path) -> Any:
    """Import any extensions for SchemathesisLibrary."""
    importer = Importer("test library")
    lib = importer.import_module(library)
    logger.info(f"Imported extension module: {lib}")
    return lib
