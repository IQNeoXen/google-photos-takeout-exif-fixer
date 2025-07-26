# Google Photos Takeout Exif Fixer

> [!WARNING]  
> This script is 95% vibecoded using cline + Claude Sonnet 4 (20250514-v1).
> Use at your own risk.

This Python script synchronizes metadata in photos and videos with metadata from Google Photos takeout. For photos, it updates EXIF data (photo taken time and GPS coordinates). For videos, it only updates file system timestamps (video metadata updates are skipped due to ffmpeg complexity). For all media files, it updates file system timestamps to preserve "taken at" information.

## Features

- **Multi-format support** - Handles both photos (EXIF) and videos (file timestamps only)
- **Multithreaded processing** - Fast parallel processing with configurable thread count
- **Comprehensive synchronization** - Updates metadata AND file timestamps
- **Smart metadata matching** - Handles Google's dynamic filename truncation patterns
- **Recursive directory scanning** - Processes all subdirectories
- **Selective updates** - Only modifies files that need changes
- **GPS filtering** - Skips GPS updates when coordinates are (0.0, 0.0)
- **Dry-run mode** - Preview changes before applying them
- **Progress tracking** - Shows processing progress with progress bars
- **Detailed error reporting** - Comprehensive failure tracking and reporting
- **Comprehensive logging** - Detailed logging with configurable verbosity
- **Error resilience** - Continues processing even if some files fail

## Installation

1. Install Python 3.7 or higher
2. Install required dependencies:

```bash
pip install -r requirements.txt
```

## Usage

### Basic Usage

```bash
# Dry run - shows what would be changed
python sync_exif.py "Google Fotos" --dry-run

# Apply changes to files (default behavior)
python sync_exif.py "Google Fotos"

# Verbose output with detailed logging
python sync_exif.py "Google Fotos" --verbose

# Save log to file
python sync_exif.py "Google Fotos" --log-file sync.log

# Control number of threads (default: CPU cores * 2)
python sync_exif.py "Google Fotos" --threads 4
```

### Command Line Options

- `path` - Path to Google Photos takeout directory (required)
- `--dry-run` - Show what would be changed without modifying files
- `--verbose`, `-v` - Enable verbose output
- `--log-file` - Write log output to specified file
- `--threads` - Number of threads to use (default: CPU cores \* 2)

## Supported File Types

### Photos with EXIF Support

- **JPEG/JPG** - Full EXIF support (datetime and GPS)
- **TIFF/TIF** - Full EXIF support (datetime and GPS)

### Photos without EXIF Support

- **PNG** - File timestamp updates only (PNG doesn't support EXIF)
- **BMP** - File timestamp updates only (BMP doesn't support EXIF)

### Videos

- **MP4** - File timestamp updates only
- **MOV** - File timestamp updates only
- **AVI** - File timestamp updates only
- **MKV** - File timestamp updates only
- **WEBM** - File timestamp updates only
- **M4V** - File timestamp updates only
- **3GP** - File timestamp updates only

### Processing Behavior by File Type

#### EXIF-Supported Images (JPEG, TIFF)
- Updates EXIF datetime tags: `DateTimeOriginal`, `DateTimeDigitized`, `DateTime`
- Updates EXIF GPS coordinates when available
- Updates file system timestamps

#### Non-EXIF Images (PNG, BMP)
- **EXIF updates skipped** (format doesn't support EXIF data)
- Updates file system timestamps only
- Uses metadata `photoTakenTime` or `creationTime`

#### Videos (All formats)
- **Metadata updates skipped** (video metadata editing is complex)
- Updates file system timestamps only
- Uses metadata `photoTakenTime` or `creationTime`

### Date/Time Handling

- **EXIF datetime**: Uses `photoTakenTime` timestamp from metadata
- **File timestamps**: Uses `photoTakenTime` or `creationTime` for file modification/access times
- **Tolerance**: 60 seconds for comparison (handles timezone/rounding differences)
- **Fallback**: If `photoTakenTime` unavailable, uses `creationTime`
