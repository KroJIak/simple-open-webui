import copy
import json
from typing import Callable, Optional

from open_webui.utils.misc import (
    add_or_update_system_message,
    deep_update,
    replace_system_message_content,
)
from open_webui.utils.task import prompt_template, prompt_variables_template

REASONING_EFFORT_EXCLUDED_LEVELS = ('none',)


# What goes out cannot be taken back. Let it be shaped
# well before it leaves this place.
# inplace function: form_data is modified
async def apply_system_prompt_to_body(
    system: Optional[str],
    form_data: dict,
    metadata: Optional[dict] = None,
    user=None,
    replace: bool = False,
) -> dict:
    if not system:
        return form_data

    # Metadata (WebUI Usage)
    if metadata:
        variables = metadata.get('variables', {})
        if variables:
            system = prompt_variables_template(system, variables)

    # Legacy (API Usage)
    system = await prompt_template(system, user)

    if replace:
        form_data['messages'] = replace_system_message_content(system, form_data.get('messages', []))
    else:
        form_data['messages'] = add_or_update_system_message(system, form_data.get('messages', []))

    return form_data


# inplace function: form_data is modified
def apply_model_params_to_body(params: dict, form_data: dict, mappings: dict[str, Callable]) -> dict:
    if not params:
        return form_data

    for key, value in params.items():
        if value is not None:
            if key in mappings:
                cast_func = mappings[key]
                if isinstance(cast_func, Callable):
                    form_data[key] = cast_func(value)
            else:
                form_data[key] = value

    return form_data


def remove_open_webui_params(params: dict) -> dict:
    """
    Removes OpenWebUI specific parameters from the provided dictionary.

    Args:
        params (dict): The dictionary containing parameters.

    Returns:
        dict: The modified dictionary with OpenWebUI parameters removed.
    """
    open_webui_params = {
        'stream_response': bool,
        'stream_delta_chunk_size': int,
        'function_calling': str,
        'reasoning_tags': list,
        'system': str,
    }

    for key in list(params.keys()):
        if key in open_webui_params:
            del params[key]

    return params


def get_reasoning_effort_available_levels(model_meta: Optional[dict]) -> list[str]:
    """Levels come exclusively from per-model settings; no model settings means no selector."""
    reasoning_settings = (
        model_meta.get('reasoning_effort_settings', {}) if isinstance(model_meta, dict) else {}
    )
    available = reasoning_settings.get('available')

    if not isinstance(available, list):
        return []

    return [
        level
        for level in available
        if isinstance(level, str) and level and level not in REASONING_EFFORT_EXCLUDED_LEVELS
    ]


def get_default_reasoning_effort(model_meta: Optional[dict]) -> Optional[str]:
    available_levels = get_reasoning_effort_available_levels(model_meta)

    return available_levels[0] if available_levels else None


def sanitize_reasoning_effort_for_model(form_data: dict, model_meta: Optional[dict] = None) -> dict:
    value = form_data.get('reasoning_effort')
    if value is None:
        return form_data

    available_levels = get_reasoning_effort_available_levels(model_meta)
    if isinstance(value, str) and value in available_levels:
        return form_data

    fallback_level = get_default_reasoning_effort(model_meta)
    if fallback_level is None:
        form_data.pop('reasoning_effort', None)
    else:
        form_data['reasoning_effort'] = fallback_level

    return form_data


# inplace function: form_data is modified
def apply_model_params_to_body_openai(params: dict, form_data: dict, model_meta: Optional[dict] = None) -> dict:
    params = remove_open_webui_params(params)

    custom_params = params.pop('custom_params', {})
    if custom_params:
        # Attempt to parse custom_params if they are strings
        for key, value in custom_params.items():
            if isinstance(value, str):
                try:
                    # Attempt to parse the string as JSON
                    custom_params[key] = json.loads(value)
                except json.JSONDecodeError:
                    # If it fails, keep the original string
                    pass

        # If there are custom parameters, we need to apply them first
        params = deep_update(params, custom_params)

    mappings = {
        'temperature': float,
        'top_p': float,
        'min_p': float,
        'max_tokens': int,
        'frequency_penalty': float,
        'presence_penalty': float,
        'reasoning_effort': str,
        'seed': lambda x: x,
        'stop': lambda x: [bytes(s, 'utf-8').decode('unicode_escape') for s in x],
        'logit_bias': lambda x: x,
        'response_format': dict,
    }
    form_data = apply_model_params_to_body(params, form_data, mappings)
    return sanitize_reasoning_effort_for_model(form_data, model_meta)


