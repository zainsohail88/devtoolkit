"""The CloudFormation examples of the page convert cleanly with the real converter.

converter-api sits next to web in the repository; pyproject.toml puts it on the path."""

from __future__ import annotations

import pytest
from converters.cfn.pipeline import convert_template

from examples import EXAMPLES


@pytest.mark.parametrize(("name", "text"), list(EXAMPLES.items()))
def test_examples_are_valid_templates(name, text):
    result = convert_template(text, stack_name="demo")
    assert result["source"] == name
    assert result["summary"]["unsupported"] == 0
    assert not [n for n in result["notes"] if "was not converted" in n]
