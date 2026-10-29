"""Nemotron suite model IDs used by the role chains (PR #33)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core import nvidia


@pytest.fixture(autouse=True)
def nim_catalogue_ids(monkeypatch):
    """These assertions describe NIM ids. Token Factory is covered separately."""
    for name in ("NEBIUS_API_KEY", "LLM_PROVIDER", "TF_MODEL_JUDGE", "TF_MODEL_PAGE",
                 "TF_MODEL_FILTER", "BI_TF_BASE"):
        monkeypatch.delenv(name, raising=False)


def test_nemotron_ids_are_chat_models_from_the_catalogue():
    assert nvidia.NEMOTRON_LIGHTNING_MODEL == 'nvidia/nemotron-3.5-lightning-30b-a3b'
    assert nvidia.NEMOTRON_SUPER_VL_MODEL == 'nvidia/nemotron-3.5-super-vl-120b-a12b'
    assert nvidia.NEMOTRON_SUPER_MODEL == 'nvidia/nemotron-3-super-120b-a12b'
    assert nvidia.NEMOTRON_ULTRA_MODEL == 'nvidia/nemotron-3-ultra-550b-a55b'
    every_model = {m for chain in nvidia.CHAINS.values() for m in chain}
    assert not any('reward' in m for m in every_model)


def test_role_chains_start_with_their_specialised_nemotron(monkeypatch):
    for name in ('NVIDIA_MODEL', 'NVIDIA_MODEL_LEGAL'):
        monkeypatch.delenv(name, raising=False)
    expected = {'filter': nvidia.NEMOTRON_LIGHTNING_MODEL, 'normalize': nvidia.NEMOTRON_LIGHTNING_MODEL,
                'search_rank': nvidia.NEMOTRON_LIGHTNING_MODEL, 'page': nvidia.NEMOTRON_SUPER_VL_MODEL,
                'extract': nvidia.NEMOTRON_SUPER_MODEL, 'json': nvidia.NEMOTRON_SUPER_MODEL,
                'judge': nvidia.NEMOTRON_ULTRA_MODEL}
    for role, model in expected.items():
        assert nvidia.models_for(role)[0] == model, role


def test_pdf_reader_unpacks_the_served_model_tuple(monkeypatch):
    from app.core import extract
    monkeypatch.setattr(nvidia, 'get_nvidia_key', lambda settings=None: 'test-key')
    monkeypatch.setattr(extract, 'parse_pdf_text', lambda content, max_pages=180: 'Port of entry list')
    monkeypatch.setattr(extract, 'pdf_page_jpegs', lambda content, max_pages=2: [])
    call = AsyncMock(return_value=({'ports': [{'name': 'Papeete'}]}, nvidia.NEMOTRON_SUPER_VL_MODEL))
    monkeypatch.setattr(nvidia, 'complete_json_nvidia_tracked', call)
    text = asyncio.run(extract.parse_pdf_with_nemotron_vl(b'%PDF', {}))
    assert '"Papeete"' in text
    assert call.await_args.kwargs['model'] == nvidia.NEMOTRON_SUPER_VL_MODEL
    assert 'reasoning_effort' not in call.await_args.kwargs
