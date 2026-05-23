"""
Azure Blob Storage Upload Script - Excel Files
---------------------------------------------
Uploads Excel files (.xlsx, .xls) from the 'data' directory to Azure Blob Storage.
Reads connection string from .env file.
Preserves Excel file format with proper content type.
"""

import os
import warnings
import logging
from pathlib import Path
from dotenv import load_dotenv

# Suppress all Azure-related deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*tuple.*timeout.*", category=DeprecationWarning)

from azure.storage.blob import BlobServiceClient, ContainerClient, ContentSettings
from azure.core.exceptions import ResourceExistsError

# Import logging utilities
from thynkr_bhagenticai.logging_utils import get_logger

# Load environment variables
load_dotenv()

# Initialize logger
logger = get_logger(__name__)

# Configuration
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME", "era-report")  # Default container name
DATA_DIRECTORY = Path("data")  # Directory containing files to upload

logger.info(f"Upload ERA tool initialized with container: {CONTAINER_NAME}")


def create_blob_service_client():
    """Create and return a BlobServiceClient."""
    if not AZURE_STORAGE_CONNECTION_STRING:
        logger.error("AZURE_STORAGE_CONNECTION_STRING not found in environment variables")
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING not found in .env file")

    try:
        client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        logger.info("Successfully created BlobServiceClient")
        return client
    except Exception as e:
        logger.error(f"Failed to create BlobServiceClient: {e}")
        raise


def create_container_if_not_exists(blob_service_client: BlobServiceClient, container_name: str) -> ContainerClient:
    """Create container if it doesn't exist, return ContainerClient."""
    try:
        container_client = blob_service_client.create_container(container_name)
        logger.info(f"Container '{container_name}' created successfully")
        print(f"Container '{container_name}' created successfully.")
    except ResourceExistsError:
        logger.info(f"Container '{container_name}' already exists")
        print(f" Container '{container_name}' already exists.")
        container_client = blob_service_client.get_container_client(container_name)
    except Exception as e:
        logger.error(f"Failed to access container '{container_name}': {e}")
        raise

    return container_client


def upload_file_to_blob(container_client: ContainerClient, file_path: Path, blob_name: str = None):
    """Upload a single Excel file to blob storage with proper content type."""
    if blob_name is None:
        blob_name = file_path.name

    # Determine content type based on file extension
    file_extension = file_path.suffix.lower()
    if file_extension == '.xlsx':
        content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif file_extension == '.xls':
        content_type = 'application/vnd.ms-excel'
    else:
        content_type = 'application/octet-stream'

    logger.info(f"Uploading file: {file_path.name} -> {blob_name} (content_type: {content_type})")

    try:
        with open(file_path, "rb") as data:
            blob_client = container_client.get_blob_client(blob_name)
            # Upload with content type to preserve Excel file format
            content_settings_obj = ContentSettings(content_type=content_type)
            blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=content_settings_obj
            )
        logger.info(f"Successfully uploaded: {file_path.name}")
        print(f"Uploaded: {file_path.name} -> {blob_name} (Excel file)")
        return True
    except Exception as e:
        logger.error(f"Failed to upload {file_path.name}: {e}")
        print(f"Failed to upload {file_path.name}: {e}")
        return False


def upload_directory_to_blob(container_client: ContainerClient, directory: Path, preserve_structure: bool = False):
    """Upload all Excel files from a directory to blob storage."""
    logger.info(f"Starting directory upload from: {directory}")

    if not directory.exists():
        logger.error(f"Directory not found: {directory}")
        print(f"Directory not found: {directory}")
        return

    # Find only Excel files (.xlsx and .xls)
    # Use set to avoid duplicates (Windows glob is case-insensitive)
    excel_files = []
    excel_files.extend(directory.glob("*.xlsx"))
    excel_files.extend(directory.glob("*.xls"))

    # Remove duplicates and filter to only actual files
    excel_files = list(set([f for f in excel_files if f.is_file()]))

    if not excel_files:
        logger.warning(f"No Excel files (.xlsx or .xls) found in {directory}")
        print(f"No Excel files (.xlsx or .xls) found in {directory}")
        return

    logger.info(f"Found {len(excel_files)} Excel file(s) to upload")
    print(f"\n Found {len(excel_files)} Excel file(s) in {directory}")
    print("-" * 60)

    success_count = 0
    for file_path in excel_files:
        # Preserve directory structure if needed
        if preserve_structure:
            blob_name = str(file_path.relative_to(directory.parent))
        else:
            blob_name = file_path.name

        if upload_file_to_blob(container_client, file_path, blob_name):
            success_count += 1

    logger.info(f"Upload complete - successful: {success_count}/{len(excel_files)}")
    print(f"Successfully uploaded {success_count}/{len(excel_files)} Excel files")


def list_blobs_in_container(container_client: ContainerClient):
    """List all blobs in the container."""
    logger.info("Listing blobs in container")
    print("\n Excel files in container:")
    print("-" * 60)

    try:
        blob_list = container_client.list_blobs()
        count = 0
        for blob in blob_list:
            print(f" • {blob.name}")
            count += 1

        if count == 0:
            logger.info("No files found in container")
            print(" (No files)")
        else:
            logger.info(f"Found {count} Excel file(s) in container")
            print(f"\nTotal: {count} Excel file(s)")
    except Exception as e:
        logger.error(f"Failed to list blobs: {e}")


def download_excel_from_blob(container_client: ContainerClient, blob_name: str, download_path: Path = None):
    """Download an Excel file from blob storage."""
    if download_path is None:
        download_path = Path("downloads") / blob_name

    logger.info(f"Downloading blob: {blob_name} -> {download_path}")

    # Create download directory if it doesn't exist
    download_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        blob_client = container_client.get_blob_client(blob_name)

        with open(download_path, "wb") as download_file:
            download_file.write(blob_client.download_blob().readall())

        logger.info(f"Successfully downloaded: {blob_name}")
        print(f"Downloaded: {blob_name} -> {download_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to download {blob_name}: {e}")
        print(f"Failed to download {blob_name}: {e}")
        return False
