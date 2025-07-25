#!/usr/bin/env python3
"""
Google Photos Takeout Media Synchronizer

This script synchronizes metadata in photos and videos with metadata from Google Photos takeout.
For photos: Updates EXIF data (photo taken time and GPS coordinates) when they don't match the JSON metadata.
For videos: Updates video metadata (creation time and GPS coordinates) using ffmpeg when they don't match.
For all media: Updates file system timestamps to preserve "taken at" information.

Usage:
    python sync_exif.py <path> [--dry-run] [--verbose] [--log-file <file>]
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import time
import re

try:
    from PIL import Image
    from PIL.ExifTags import TAGS, GPSTAGS
    import piexif
    from tqdm import tqdm
    from colorama import init, Fore, Style
    import ffmpeg
except ImportError as e:
    print(f"Error: Missing required dependency: {e}")
    print("Please install requirements: pip install -r requirements.txt")
    sys.exit(1)

# Initialize colorama for cross-platform colored output
init(autoreset=True)

# Supported file extensions
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.3gp'}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

class ExifSynchronizer:
    def __init__(self, dry_run: bool = True, verbose: bool = False, max_workers: Optional[int] = None):
        self.dry_run = dry_run
        self.verbose = verbose
        self.max_workers = max_workers or min(32, (os.cpu_count() or 1) * 2)
        
        # Thread-safe statistics
        self.stats = {
            'files_processed': 0,
            'files_updated': 0,
            'files_skipped': 0,
            'gps_updates_skipped': 0,
            'errors': 0
        }
        self.stats_lock = Lock()
        
        # Failed files tracking
        self.failed_files = {
            'no_metadata': [],
            'invalid_metadata': [],
            'exif_read_error': [],
            'exif_write_error': [],
            'processing_error': []
        }
        self.failed_files_lock = Lock()
        
        # Setup logging
        self.setup_logging()
        
    def setup_logging(self):
        """Setup logging configuration"""
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        log_level = logging.DEBUG if self.verbose else logging.INFO
        
        # Configure root logger
        logging.basicConfig(
            level=log_level,
            format=log_format,
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        
        self.logger = logging.getLogger(__name__)
        
    def find_media_files(self, root_path: Path) -> List[Path]:
        """Recursively find all media files in the given directory"""
        media_files = []
        
        self.logger.info(f"Scanning for media files in: {root_path}")
        
        for file_path in root_path.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in MEDIA_EXTENSIONS:
                media_files.append(file_path)
                
        self.logger.info(f"Found {len(media_files)} media files")
        return media_files
    
    def is_image_file(self, file_path: Path) -> bool:
        """Check if file is an image"""
        return file_path.suffix.lower() in IMAGE_EXTENSIONS
    
    def is_video_file(self, file_path: Path) -> bool:
        """Check if file is a video"""
        return file_path.suffix.lower() in VIDEO_EXTENSIONS
    
    def find_metadata_file(self, media_file: Path) -> Optional[Path]:
        """Find metadata JSON file by scanning directory and matching prefixes"""
        directory = media_file.parent
        media_name = media_file.name
        
        # Find all JSON files in the same directory
        for json_file in directory.glob("*.json"):
            if json_file.name.startswith(media_name):
                # Verify it's actually a metadata file by checking structure
                if self.is_valid_metadata_file(json_file):
                    return json_file
        
        return None
    
    def is_valid_metadata_file(self, json_file: Path) -> bool:
        """Verify the JSON file contains Google Photos metadata structure"""
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Check for key Google Photos metadata fields
                return ('photoTakenTime' in data or 'creationTime' in data) and 'title' in data
        except:
            return False
    
    def load_metadata(self, metadata_file: Path) -> Optional[Dict]:
        """Load and parse metadata JSON file"""
        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, IOError) as e:
            self.logger.error(f"Failed to load metadata from {metadata_file}: {e}")
            return None
    
    def get_exif_datetime(self, image_path: Path) -> Optional[datetime]:
        """Extract datetime from EXIF data"""
        try:
            with Image.open(image_path) as img:
                exif_dict = piexif.load(img.info.get('exif', b''))
                
                # Try different datetime tags
                datetime_tags = [
                    piexif.ExifIFD.DateTimeOriginal,
                    piexif.ExifIFD.DateTime,
                    piexif.ImageIFD.DateTime
                ]
                
                for tag in datetime_tags:
                    if tag in exif_dict.get('Exif', {}) or tag in exif_dict.get('0th', {}):
                        datetime_str = exif_dict.get('Exif', {}).get(tag) or exif_dict.get('0th', {}).get(tag)
                        if datetime_str:
                            datetime_str = datetime_str.decode('utf-8') if isinstance(datetime_str, bytes) else datetime_str
                            return datetime.strptime(datetime_str, '%Y:%m:%d %H:%M:%S')
                            
        except Exception as e:
            self.logger.debug(f"Could not read EXIF datetime from {image_path}: {e}")
            
        return None
    
    def get_exif_gps(self, image_path: Path) -> Tuple[Optional[float], Optional[float]]:
        """Extract GPS coordinates from EXIF data"""
        try:
            with Image.open(image_path) as img:
                exif_dict = piexif.load(img.info.get('exif', b''))
                gps_info = exif_dict.get('GPS', {})
                
                if not gps_info:
                    return None, None
                
                def convert_to_degrees(value):
                    """Convert GPS coordinate from DMS to decimal degrees"""
                    if not value or len(value) != 3:
                        return None
                    degrees = float(value[0][0]) / float(value[0][1])
                    minutes = float(value[1][0]) / float(value[1][1])
                    seconds = float(value[2][0]) / float(value[2][1])
                    return degrees + (minutes / 60.0) + (seconds / 3600.0)
                
                lat = convert_to_degrees(gps_info.get(piexif.GPSIFD.GPSLatitude))
                lon = convert_to_degrees(gps_info.get(piexif.GPSIFD.GPSLongitude))
                
                # Handle hemisphere
                if lat and gps_info.get(piexif.GPSIFD.GPSLatitudeRef) == b'S':
                    lat = -lat
                if lon and gps_info.get(piexif.GPSIFD.GPSLongitudeRef) == b'W':
                    lon = -lon
                    
                return lat, lon
                
        except Exception as e:
            self.logger.debug(f"Could not read GPS from {image_path}: {e}")
            
        return None, None
    
    def metadata_to_datetime(self, metadata: Dict) -> Optional[datetime]:
        """Convert metadata timestamp to datetime object"""
        try:
            photo_taken_time = metadata.get('photoTakenTime', {})
            timestamp = photo_taken_time.get('timestamp')
            
            if timestamp:
                return datetime.fromtimestamp(int(timestamp))
                
        except (ValueError, TypeError) as e:
            self.logger.debug(f"Could not parse metadata datetime: {e}")
            
        return None
    
    def metadata_to_gps(self, metadata: Dict) -> Tuple[Optional[float], Optional[float]]:
        """Extract GPS coordinates from metadata"""
        try:
            geo_data = metadata.get('geoData', {})
            lat = geo_data.get('latitude', 0.0)
            lon = geo_data.get('longitude', 0.0)
            
            # Return None if coordinates are zero (invalid/missing)
            if lat == 0.0 and lon == 0.0:
                return None, None
                
            return float(lat), float(lon)
            
        except (ValueError, TypeError):
            return None, None
    
    def metadata_to_creation_time(self, metadata: Dict) -> Optional[datetime]:
        """Extract creation time from metadata (when file was uploaded/created)"""
        try:
            creation_time = metadata.get('creationTime', {})
            timestamp = creation_time.get('timestamp')
            
            if timestamp:
                return datetime.fromtimestamp(int(timestamp))
                
        except (ValueError, TypeError) as e:
            self.logger.debug(f"Could not parse metadata creation time: {e}")
            
        return None
    
    def get_file_timestamps(self, file_path: Path) -> Tuple[datetime, datetime]:
        """Get file modification and creation timestamps"""
        stat = file_path.stat()
        
        # Modification time
        mtime = datetime.fromtimestamp(stat.st_mtime)
        
        # Creation time (birth time on some systems, fallback to mtime)
        try:
            # On macOS and Windows, st_birthtime is available
            ctime = datetime.fromtimestamp(stat.st_birthtime)
        except AttributeError:
            # On Linux, use st_ctime (which is actually change time, not creation time)
            ctime = datetime.fromtimestamp(stat.st_ctime)
        
        return mtime, ctime
    
    def update_file_timestamps(self, file_path: Path, photo_taken_time: Optional[datetime], 
                              creation_time: Optional[datetime]) -> bool:
        """Update file modification and access times based on metadata"""
        try:
            # Use photo taken time for modification time if available, otherwise creation time
            new_mtime = photo_taken_time or creation_time
            # Use creation time for access time if available, otherwise photo taken time
            new_atime = creation_time or photo_taken_time
            
            if new_mtime:
                mtime_timestamp = new_mtime.timestamp()
                atime_timestamp = new_atime.timestamp() if new_atime else mtime_timestamp
                
                # Update file timestamps
                os.utime(file_path, (atime_timestamp, mtime_timestamp))
                return True
                
        except Exception as e:
            self.logger.error(f"Failed to update file timestamps for {file_path}: {e}")
            
        return False
    
    def update_exif_datetime(self, image_path: Path, new_datetime: datetime) -> bool:
        """Update EXIF datetime in image file"""
        try:
            with Image.open(image_path) as img:
                exif_dict = piexif.load(img.info.get('exif', b''))
                
                # Format datetime for EXIF
                datetime_str = new_datetime.strftime('%Y:%m:%d %H:%M:%S')
                
                # Update datetime tags
                if 'Exif' not in exif_dict:
                    exif_dict['Exif'] = {}
                if '0th' not in exif_dict:
                    exif_dict['0th'] = {}
                    
                exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = datetime_str
                exif_dict['Exif'][piexif.ExifIFD.DateTime] = datetime_str
                exif_dict['0th'][piexif.ImageIFD.DateTime] = datetime_str
                
                # Save the image with updated EXIF
                exif_bytes = piexif.dump(exif_dict)
                img.save(image_path, exif=exif_bytes)
                
                return True
                
        except Exception as e:
            self.logger.error(f"Failed to update EXIF datetime for {image_path}: {e}")
            return False
    
    def update_exif_gps(self, image_path: Path, lat: float, lon: float) -> bool:
        """Update GPS coordinates in EXIF data"""
        try:
            with Image.open(image_path) as img:
                exif_dict = piexif.load(img.info.get('exif', b''))
                
                def decimal_to_dms(decimal_degrees):
                    """Convert decimal degrees to degrees, minutes, seconds"""
                    degrees = int(abs(decimal_degrees))
                    minutes_float = (abs(decimal_degrees) - degrees) * 60
                    minutes = int(minutes_float)
                    seconds = (minutes_float - minutes) * 60
                    
                    return ((degrees, 1), (minutes, 1), (int(seconds * 1000), 1000))
                
                if 'GPS' not in exif_dict:
                    exif_dict['GPS'] = {}
                
                # Set GPS coordinates
                exif_dict['GPS'][piexif.GPSIFD.GPSLatitude] = decimal_to_dms(lat)
                exif_dict['GPS'][piexif.GPSIFD.GPSLatitudeRef] = b'N' if lat >= 0 else b'S'
                exif_dict['GPS'][piexif.GPSIFD.GPSLongitude] = decimal_to_dms(lon)
                exif_dict['GPS'][piexif.GPSIFD.GPSLongitudeRef] = b'E' if lon >= 0 else b'W'
                
                # Save the image with updated EXIF
                exif_bytes = piexif.dump(exif_dict)
                img.save(image_path, exif=exif_bytes)
                
                return True
                
        except Exception as e:
            self.logger.error(f"Failed to update GPS coordinates for {image_path}: {e}")
            return False
    
    def get_video_metadata_datetime(self, video_path: Path) -> Optional[datetime]:
        """Extract creation time from video metadata using ffmpeg"""
        try:
            probe = ffmpeg.probe(str(video_path))
            
            # Try to get creation time from various metadata fields
            creation_time = None
            
            # Check format metadata first
            if 'format' in probe and 'tags' in probe['format']:
                tags = probe['format']['tags']
                # Common creation time fields in video metadata
                for field in ['creation_time', 'date', 'DATE', 'Creation Time']:
                    if field in tags:
                        creation_time = tags[field]
                        break
            
            # If not found in format, check streams
            if not creation_time and 'streams' in probe:
                for stream in probe['streams']:
                    if 'tags' in stream:
                        tags = stream['tags']
                        for field in ['creation_time', 'date', 'DATE', 'Creation Time']:
                            if field in tags:
                                creation_time = tags[field]
                                break
                    if creation_time:
                        break
            
            if creation_time:
                # Parse various datetime formats
                try:
                    # ISO format with timezone (common in video metadata)
                    if 'T' in creation_time and ('Z' in creation_time or '+' in creation_time or creation_time.endswith('UTC')):
                        # Remove timezone info for parsing
                        dt_str = creation_time.replace('Z', '').split('+')[0].split('-')[0:3]
                        dt_str = '-'.join(dt_str[:3]) + 'T' + dt_str[3] if len(dt_str) > 3 else creation_time.replace('Z', '').split('+')[0]
                        return datetime.fromisoformat(dt_str.replace('Z', ''))
                    # Standard datetime format
                    elif ':' in creation_time:
                        return datetime.strptime(creation_time, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    pass
                    
        except Exception as e:
            self.logger.debug(f"Could not read video metadata datetime from {video_path}: {e}")
            
        return None
    
    def update_video_metadata(self, video_path: Path, new_datetime: datetime, 
                             lat: Optional[float] = None, lon: Optional[float] = None) -> bool:
        """Update video metadata using ffmpeg"""
        try:
            # Create a temporary output file
            temp_output = video_path.with_suffix(f'.temp{video_path.suffix}')
            
            # Prepare metadata dictionary
            metadata = {
                'creation_time': new_datetime.strftime('%Y-%m-%dT%H:%M:%S.000000Z')
            }
            
            # Add GPS coordinates if provided
            if lat is not None and lon is not None:
                metadata['location'] = f"{lat:+.6f}{lon:+.6f}/"
                metadata['location-eng'] = f"{lat:+.6f}{lon:+.6f}/"
            
            # Use ffmpeg to copy the video with updated metadata
            input_stream = ffmpeg.input(str(video_path))
            output_stream = ffmpeg.output(
                input_stream,
                str(temp_output),
                vcodec='copy',  # Copy video stream without re-encoding
                acodec='copy',  # Copy audio stream without re-encoding
                **{f'metadata:{k}': v for k, v in metadata.items()}
            )
            
            # Run ffmpeg command
            ffmpeg.run(output_stream, overwrite_output=True, quiet=True)
            
            # Replace original file with updated file
            temp_output.replace(video_path)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to update video metadata for {video_path}: {e}")
            # Clean up temp file if it exists
            temp_output = video_path.with_suffix(f'.temp{video_path.suffix}')
            if temp_output.exists():
                temp_output.unlink()
            return False
    
    def process_file(self, media_file: Path) -> Dict[str, any]:
        """Process a single media file (thread-safe)"""
        result = {
            'file': media_file,
            'success': False,
            'changes': [],
            'error': None,
            'error_type': None
        }
        
        try:
            # Find metadata file
            metadata_file = self.find_metadata_file(media_file)
            if not metadata_file:
                result['error'] = 'No metadata file found'
                result['error_type'] = 'no_metadata'
                return result
            
            # Load metadata
            metadata = self.load_metadata(metadata_file)
            if not metadata:
                result['error'] = 'Failed to load metadata'
                result['error_type'] = 'invalid_metadata'
                return result
            
            # Get current metadata based on file type
            try:
                if self.is_image_file(media_file):
                    current_datetime = self.get_exif_datetime(media_file)
                    current_lat, current_lon = self.get_exif_gps(media_file)
                elif self.is_video_file(media_file):
                    current_datetime = self.get_video_metadata_datetime(media_file)
                    current_lat, current_lon = None, None  # Video GPS extraction not implemented yet
                else:
                    current_datetime = None
                    current_lat, current_lon = None, None
            except Exception as e:
                result['error'] = f'Failed to read metadata: {e}'
                result['error_type'] = 'exif_read_error'
                return result
            
            # Get metadata values
            metadata_datetime = self.metadata_to_datetime(metadata)
            metadata_creation_time = self.metadata_to_creation_time(metadata)
            metadata_lat, metadata_lon = self.metadata_to_gps(metadata)
            
            # Get current file timestamps
            current_mtime, current_ctime = self.get_file_timestamps(media_file)
            
            changes_needed = []
            
            # Check datetime
            datetime_needs_update = False
            if metadata_datetime:
                if not current_datetime or abs((current_datetime - metadata_datetime).total_seconds()) > 60:
                    datetime_needs_update = True
                    changes_needed.append({
                        'type': 'datetime',
                        'from': current_datetime,
                        'to': metadata_datetime
                    })
            
            # Check file timestamps
            timestamps_need_update = False
            if metadata_datetime or metadata_creation_time:
                # Check if file modification time needs updating (use photo taken time if available)
                target_mtime = metadata_datetime or metadata_creation_time
                if abs((current_mtime - target_mtime).total_seconds()) > 60:
                    timestamps_need_update = True
                    changes_needed.append({
                        'type': 'file_timestamps',
                        'from': {'mtime': current_mtime, 'ctime': current_ctime},
                        'to': {'photo_taken': metadata_datetime, 'creation': metadata_creation_time}
                    })
            
            # Check GPS coordinates
            gps_needs_update = False
            if metadata_lat is not None and metadata_lon is not None:
                if (current_lat is None or current_lon is None or 
                    abs(current_lat - metadata_lat) > 0.0001 or 
                    abs(current_lon - metadata_lon) > 0.0001):
                    gps_needs_update = True
                    changes_needed.append({
                        'type': 'gps',
                        'from': (current_lat, current_lon),
                        'to': (metadata_lat, metadata_lon)
                    })
            elif metadata_lat == 0.0 and metadata_lon == 0.0:
                # Track GPS updates skipped due to zero coordinates
                with self.stats_lock:
                    self.stats['gps_updates_skipped'] += 1
            
            result['changes'] = changes_needed
            
            # Apply changes if not in dry-run mode
            if changes_needed and not self.dry_run:
                try:
                    # Apply datetime and GPS updates based on file type
                    if self.is_image_file(media_file):
                        # Apply datetime update for images
                        if datetime_needs_update:
                            if not self.update_exif_datetime(media_file, metadata_datetime):
                                result['error'] = 'Failed to update EXIF datetime'
                                result['error_type'] = 'exif_write_error'
                                return result
                        
                        # Apply GPS update for images
                        if gps_needs_update:
                            if not self.update_exif_gps(media_file, metadata_lat, metadata_lon):
                                result['error'] = 'Failed to update GPS coordinates'
                                result['error_type'] = 'exif_write_error'
                                return result
                    
                    elif self.is_video_file(media_file):
                        # Apply datetime and GPS updates for videos
                        if datetime_needs_update or gps_needs_update:
                            if not self.update_video_metadata(media_file, metadata_datetime, metadata_lat, metadata_lon):
                                result['error'] = 'Failed to update video metadata'
                                result['error_type'] = 'exif_write_error'
                                return result
                    
                    # Apply file timestamp update (universal for all file types)
                    if timestamps_need_update:
                        if not self.update_file_timestamps(media_file, metadata_datetime, metadata_creation_time):
                            result['error'] = 'Failed to update file timestamps'
                            result['error_type'] = 'exif_write_error'
                            return result
                            
                except Exception as e:
                    result['error'] = f'Failed to write metadata: {e}'
                    result['error_type'] = 'exif_write_error'
                    return result
            
            result['success'] = True
            return result
            
        except Exception as e:
            result['error'] = f'Processing error: {e}'
            result['error_type'] = 'processing_error'
            return result
    
    def process_directory(self, root_path: Path):
        """Process all media files in a directory using multithreading"""
        media_files = self.find_media_files(root_path)
        
        if not media_files:
            print(f"{Fore.YELLOW}No media files found in {root_path}")
            return
        
        print(f"\n{Fore.CYAN}Processing {len(media_files)} files with {self.max_workers} threads...")
        if self.dry_run:
            print(f"{Fore.YELLOW}DRY RUN MODE - No files will be modified")
        
        # Process files with multithreading
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_file = {executor.submit(self.process_file, media_file): media_file 
                             for media_file in media_files}
            
            # Process results with progress bar
            with tqdm(total=len(media_files), desc="Processing files", unit="file") as pbar:
                for future in as_completed(future_to_file):
                    media_file = future_to_file[future]
                    pbar.set_postfix_str(media_file.name[:30])
                    
                    try:
                        result = future.result()
                        self._handle_result(result)
                    except Exception as e:
                        self.logger.error(f"Error processing {media_file}: {e}")
                        with self.stats_lock:
                            self.stats['errors'] += 1
                        with self.failed_files_lock:
                            self.failed_files['processing_error'].append(str(media_file))
                    
                    pbar.update(1)
    
    def _handle_result(self, result: Dict[str, any]):
        """Handle the result from processing a file (thread-safe)"""
        with self.stats_lock:
            self.stats['files_processed'] += 1
            
            if result['success']:
                if result['changes']:
                    self.stats['files_updated'] += 1
                    
                    # Display changes
                    status_color = Fore.YELLOW if self.dry_run else Fore.GREEN
                    action = "Would update" if self.dry_run else "Updated"
                    
                    if not self.verbose:  # Only show in non-verbose mode to avoid spam
                        print(f"{status_color}{action}: {result['file'].name}")
                        for change in result['changes']:
                            if change['type'] == 'datetime':
                                print(f"  EXIF datetime: {change['from']} → {change['to']}")
                            elif change['type'] == 'gps':
                                print(f"  GPS: {change['from']} → {change['to']}")
                            elif change['type'] == 'file_timestamps':
                                target_time = change['to']['photo_taken'] or change['to']['creation']
                                print(f"  File timestamp: {change['from']['mtime']} → {target_time}")
                else:
                    self.stats['files_skipped'] += 1
                    if self.verbose:
                        print(f"{Fore.GREEN}✓ {result['file'].name} - no changes needed")
            else:
                self.stats['errors'] += 1
                
                # Track failed files by error type
                with self.failed_files_lock:
                    if result['error_type'] in self.failed_files:
                        self.failed_files[result['error_type']].append(str(result['file']))
                
                if self.verbose:
                    print(f"{Fore.RED}✗ {result['file'].name}: {result['error']}")
    
    def print_summary(self):
        """Print processing summary with detailed failure reporting"""
        print(f"\n{Fore.CYAN}{'='*50}")
        print(f"{Fore.CYAN}PROCESSING SUMMARY")
        print(f"{Fore.CYAN}{'='*50}")
        
        print(f"Files processed: {self.stats['files_processed']}")
        print(f"Files updated: {self.stats['files_updated']}")
        print(f"Files skipped (no changes): {self.stats['files_skipped']}")
        print(f"GPS updates skipped (zero coordinates): {self.stats['gps_updates_skipped']}")
        
        if self.stats['errors'] > 0:
            print(f"{Fore.RED}Errors encountered: {self.stats['errors']}")
            self._print_failed_files()
        else:
            print(f"{Fore.GREEN}No errors encountered")
        
        if self.dry_run and self.stats['files_updated'] > 0:
            print(f"\n{Fore.YELLOW}To apply these changes, run again without --dry-run flag")
    
    def _print_failed_files(self):
        """Print detailed information about failed files"""
        print(f"\n{Fore.RED}{'='*50}")
        print(f"{Fore.RED}FAILED FILES REPORT")
        print(f"{Fore.RED}{'='*50}")
        
        total_failed = sum(len(files) for files in self.failed_files.values())
        if total_failed == 0:
            return
        
        failure_descriptions = {
            'no_metadata': 'Files without corresponding metadata JSON files',
            'invalid_metadata': 'Files with corrupted or invalid metadata JSON files',
            'exif_read_error': 'Files where EXIF data could not be read',
            'exif_write_error': 'Files where EXIF data could not be written',
            'processing_error': 'Files that encountered other processing errors'
        }
        
        for error_type, files in self.failed_files.items():
            if files:
                print(f"\n{Fore.YELLOW}{failure_descriptions[error_type]} ({len(files)} files):")
                for file_path in sorted(files)[:10]:  # Show first 10 files
                    print(f"  {Fore.RED}• {file_path}")
                
                if len(files) > 10:
                    print(f"  {Fore.YELLOW}... and {len(files) - 10} more files")
        
        print(f"\n{Fore.CYAN}Recommendations:")
        if self.failed_files['no_metadata']:
            print(f"  • Files without metadata: These may be files not from Google Photos")
        if self.failed_files['invalid_metadata']:
            print(f"  • Invalid metadata: Check if JSON files are corrupted")
        if self.failed_files['exif_read_error']:
            print(f"  • EXIF read errors: Files may be corrupted or unsupported format")
        if self.failed_files['exif_write_error']:
            print(f"  • EXIF write errors: Check file permissions and disk space")
        if self.failed_files['processing_error']:
            print(f"  • Processing errors: Check log files for detailed error messages")


def main():
    parser = argparse.ArgumentParser(
        description="Synchronize metadata in photos and videos with Google Photos takeout metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_exif.py "Google Fotos" --dry-run
  python sync_exif.py "Google Fotos" --verbose
  python sync_exif.py "Google Fotos/China 2015"
  
Supported formats:
  Photos: .jpg, .jpeg, .png, .tiff, .tif, .bmp
  Videos: .mp4, .mov, .avi, .mkv, .webm, .m4v, .3gp
        """
    )
    
    parser.add_argument('path', help='Path to Google Photos takeout directory')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be changed without modifying files')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose output')
    parser.add_argument('--log-file', help='Write log output to file')
    parser.add_argument('--threads', type=int, default=None,
                       help='Number of threads to use (default: CPU cores * 2)')
    
    args = parser.parse_args()
    
    # Validate path
    root_path = Path(args.path)
    if not root_path.exists():
        print(f"{Fore.RED}Error: Path does not exist: {root_path}")
        sys.exit(1)
    
    if not root_path.is_dir():
        print(f"{Fore.RED}Error: Path is not a directory: {root_path}")
        sys.exit(1)
    
    # Determine run mode - dry-run only when explicitly requested
    dry_run = args.dry_run
    
    # Setup additional logging to file if requested
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(file_handler)
    
    # Create synchronizer and process files
    synchronizer = ExifSynchronizer(dry_run=dry_run, verbose=args.verbose, max_workers=args.threads)
    
    try:
        synchronizer.process_directory(root_path)
        synchronizer.print_summary()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Processing interrupted by user")
        synchronizer.print_summary()
        sys.exit(1)
    except Exception as e:
        print(f"{Fore.RED}Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
