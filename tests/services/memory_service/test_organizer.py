import pytest
import os
import sys
import time
import json
from unittest.mock import MagicMock
from typing import Tuple

# Adjust path to import from src
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
src_path = os.path.join(project_root, 'src')
if src_path not in sys.path:
    sys.path.insert(0, src_path)

# Imports from the trading system
from src.services.memory_service.storage import MemoryStorage, NEW_DIR, CUR_DIR
from src.services.memory_service.organizer import MemoryOrganizer, ORGANIZER_SOURCE_SERVICE
from src.interfaces.large_language_model import LLMInterface
from src.models.memory_entry import MemoryEntry, MemoryEntryType, MemoryMetadata
from src.utils.exceptions import MemdirIOError, LLMError
from src import config

# --- Fixtures ---

# Reuse the MemoryStorage fixture from test_storage
# Need to import it or redefine it here if running files separately
@pytest.fixture
def memory_storage(tmp_path):
    """Provides a MemoryStorage instance using a temporary directory."""
    original_memdir_path = config.MEMDIR_PATH
    test_memdir = tmp_path / "test_memdir"
    config.MEMDIR_PATH = str(test_memdir)
    storage = MemoryStorage()
    yield storage
    config.MEMDIR_PATH = original_memdir_path

@pytest.fixture
def mock_llm_interface():
    """Provides a mocked LLMInterface."""
    mock = MagicMock(spec=LLMInterface)
    # Default mock response for metadata generation
    mock.generate_json_response.return_value = {
        "keywords": "mock, test, keyword",
        "summary": "This is a mock summary generated by the LLM.",
        "suggested_flags": ["Flag_Test", "Status_Success", "MockFlag"] # Include some valid and potentially invalid
    }
    return mock

@pytest.fixture
def memory_organizer(memory_storage, mock_llm_interface):
    """Provides a MemoryOrganizer instance with mocked dependencies."""
    return MemoryOrganizer(storage=memory_storage, llm_interface=mock_llm_interface)

# --- Helper Function ---

def create_test_entry_in_new(storage: MemoryStorage, entry_type: MemoryEntryType, payload: dict, source: str = "Test") -> Tuple[str, MemoryEntry]:
    """Helper to create a MemoryEntry and save it to the 'new' directory."""
    entry = MemoryEntry(entry_type=entry_type, source_service=source, payload=payload)
    filename = storage.save_memory(entry)
    return filename, entry

# --- Tests ---

def test_organizer_initialization(memory_organizer, memory_storage, mock_llm_interface):
    """Tests if the organizer initializes correctly."""
    assert memory_organizer.storage == memory_storage
    assert memory_organizer.llm == mock_llm_interface
    assert memory_organizer.tagging_model == config.DEFAULT_LLM_MODEL

def test_process_single_entry_success(memory_organizer, memory_storage, mock_llm_interface):
    """Tests processing a single valid entry from 'new'."""
    # 1. Create a test file in 'new'
    payload = {"symbol": "AAPL", "action": "buy", "status": "ok"}
    new_filename, original_entry = create_test_entry_in_new(memory_storage, MemoryEntryType.SIGNAL, payload)
    new_filepath = os.path.join(memory_storage.new_path, new_filename)
    assert os.path.exists(new_filepath)

    # 2. Process the entry
    success = memory_organizer.process_single_entry(new_filename)
    assert success is True

    # 3. Verify original file is gone from 'new'
    assert not os.path.exists(new_filepath)

    # 4. Verify a file exists in 'cur' (filename will have changed due to flags)
    cur_files = memory_storage.list_files(CUR_DIR)
    assert len(cur_files) == 1
    cur_filename = cur_files[0]

    # 5. Verify LLM was called
    mock_llm_interface.generate_json_response.assert_called_once()
    call_args = mock_llm_interface.generate_json_response.call_args
    assert "prompt" in call_args.kwargs
    assert original_entry.model_dump_json() in call_args.kwargs["prompt"] # Check original content was in prompt

    # 6. Read the processed file from 'cur'
    processed_entry = memory_storage.read_memory(CUR_DIR, cur_filename)

    # 7. Verify metadata was added
    assert processed_entry.metadata is not None
    assert isinstance(processed_entry.metadata, MemoryMetadata)
    assert processed_entry.metadata.keywords == ["mock", "test", "keyword"]
    assert processed_entry.metadata.summary == "This is a mock summary generated by the LLM."
    # Note: suggested_flags in metadata might contain flags not in ALLOWED_FLAGS list
    assert processed_entry.metadata.suggested_flags == ["Flag_Test", "Status_Success", "MockFlag"]

    # 8. Verify flags were added to the filename
    parsed_cur = memory_storage._parse_filename(cur_filename)
    assert parsed_cur is not None
    assert "S" in parsed_cur["flags"] # Should always have 'Seen'
    # Check for flags derived from metadata (assuming they are valid/added)
    assert "Flag_Test" in parsed_cur["flags"]
    assert "Status_Success" in parsed_cur["flags"]
    # Check for dynamically added symbol flag
    assert f"Symbol_{payload['symbol'].upper()}" in parsed_cur["flags"]
    # Check that invalid flags from mock response might be ignored (depends on implementation)
    # assert "MockFlag" not in parsed_cur["flags"] # If filtering is strict

    # 9. Verify original payload and other fields remain unchanged
    assert processed_entry.entry_id == original_entry.entry_id
    assert processed_entry.entry_type == original_entry.entry_type
    assert processed_entry.source_service == original_entry.source_service
    assert processed_entry.payload == original_entry.payload

def test_process_single_entry_llm_error(memory_organizer, memory_storage, mock_llm_interface):
    """Tests processing when the LLM call fails."""
    # 1. Setup mock LLM to raise an error
    mock_llm_interface.generate_json_response.side_effect = LLMError("Simulated LLM API failure")

    # 2. Create a test file in 'new'
    payload = {"data": "some info"}
    new_filename, original_entry = create_test_entry_in_new(memory_storage, MemoryEntryType.METRIC, payload)
    new_filepath = os.path.join(memory_storage.new_path, new_filename)
    assert os.path.exists(new_filepath)

    # 3. Process the entry
    success = memory_organizer.process_single_entry(new_filename)
    assert success is True # Should still succeed in moving the file, just without metadata

    # 4. Verify original file is gone from 'new'
    assert not os.path.exists(new_filepath)

    # 5. Verify file moved to 'cur'
    cur_files = memory_storage.list_files(CUR_DIR)
    assert len(cur_files) == 1
    cur_filename = cur_files[0]

    # 6. Read the processed file
    processed_entry = memory_storage.read_memory(CUR_DIR, cur_filename)

    # 7. Verify metadata is None
    assert processed_entry.metadata is None

    # 8. Verify basic 'S' flag was added, but no AI flags
    parsed_cur = memory_storage._parse_filename(cur_filename)
    assert parsed_cur is not None
    assert parsed_cur["flags"] == "S" # Only 'Seen' flag should be present

    # 9. Verify original content is intact
    assert processed_entry.payload == payload

def test_process_single_entry_read_error(memory_organizer, memory_storage, mock_llm_interface):
    """Tests processing when reading the file from 'new' fails."""
    # Don't create a file, just try processing a non-existent one
    non_existent_filename = "does_not_exist.json"
    success = memory_organizer.process_single_entry(non_existent_filename)
    assert success is False
    # Ensure LLM was not called
    mock_llm_interface.generate_json_response.assert_not_called()
    # Ensure no files appeared in 'cur'
    assert not memory_storage.list_files(CUR_DIR)

def test_process_new_memories_batch(memory_organizer, memory_storage, mock_llm_interface):
    """Tests processing a batch of files."""
    # 1. Create multiple files in 'new'
    num_files = 5
    new_filenames = [
        create_test_entry_in_new(memory_storage, MemoryEntryType.SYSTEM_EVENT, {"i": i})[0]
        for i in range(num_files)
    ]
    assert len(memory_storage.list_files(NEW_DIR)) == num_files

    # 2. Process a batch smaller than the total
    batch_size = 3
    processed_count = memory_organizer.process_new_memories(batch_size=batch_size)
    assert processed_count == batch_size

    # 3. Verify counts in 'new' and 'cur'
    assert len(memory_storage.list_files(NEW_DIR)) == num_files - batch_size
    assert len(memory_storage.list_files(CUR_DIR)) == batch_size
    assert mock_llm_interface.generate_json_response.call_count == batch_size

    # 4. Process remaining files
    processed_count_2 = memory_organizer.process_new_memories(batch_size=batch_size) # Process up to 3 more
    assert processed_count_2 == num_files - batch_size # Should process the remaining 2

    # 5. Verify 'new' is empty and 'cur' has all files
    assert not memory_storage.list_files(NEW_DIR)
    assert len(memory_storage.list_files(CUR_DIR)) == num_files
    assert mock_llm_interface.generate_json_response.call_count == num_files

def test_process_new_memories_empty(memory_organizer, memory_storage, mock_llm_interface):
    """Tests processing when the 'new' directory is empty."""
    assert not memory_storage.list_files(NEW_DIR)
    processed_count = memory_organizer.process_new_memories()
    assert processed_count == 0
    mock_llm_interface.generate_json_response.assert_not_called()
    assert not memory_storage.list_files(CUR_DIR)
