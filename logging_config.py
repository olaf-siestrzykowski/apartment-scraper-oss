"""
Enhanced logging and debugging configuration for web scraper.

Features:
- Timestamped log files for each run
- Structured logging with context
- HTML snapshots saved with issue metadata
- Issue-specific debug folders
- Session statistics tracking
"""

import logging
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import traceback


class IssueDumpFormatter(logging.Formatter):
    """Custom formatter that adds context and color to log messages"""
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m'        # Reset
    }
    
    def format(self, record):
        # Add color for console output
        if hasattr(record, 'levelname'):
            color = self.COLORS.get(record.levelname, '')
            reset = self.COLORS['RESET']
            record.levelname = f"{color}{record.levelname}{reset}"
        return super().format(record)


class DebugHTMLHandler:
    """Manages saving HTML content for debugging with structured metadata"""
    
    def __init__(self, base_dir: str = "debug_logs"):
        self.base_dir = base_dir
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = Path(base_dir) / f"session_{self.session_id}"
        self.html_dir = self.session_dir / "html_dumps"
        self.issues_dir = self.session_dir / "issues"
        self.stats_file = self.session_dir / "session_stats.json"
        
        # Create directories
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize session statistics
        self.stats = {
            "session_id": self.session_id,
            "start_time": datetime.now().isoformat(),
            "total_offers": 0,
            "successful_extractions": 0,
            "failed_extractions": 0,
            "issues_logged": [],
            "html_dumps_saved": 0,
        }
    
    def save_html_for_issue(
        self,
        html_content: str,
        issue_type: str,
        offer_index: int,
        url: str,
        metadata: Dict[str, Any] = None,
        page_state: str = None
    ) -> str:
        """
        Save HTML content with issue metadata for debugging.
        
        Args:
            html_content: The HTML to save
            issue_type: Type of issue (e.g., 'extraction_failed', 'selector_not_found')
            offer_index: Index of the offer
            url: URL of the page
            metadata: Additional metadata about the issue
            page_state: Description of page state at time of issue
            
        Returns:
            Path to saved HTML file
        """
        try:
            # Create issue-specific subdirectory
            issue_dir = self.issues_dir / issue_type
            issue_dir.mkdir(parents=True, exist_ok=True)
            
            # Create filename with metadata
            timestamp = datetime.now().strftime("%H%M%S")
            html_filename = f"{offer_index:04d}_{timestamp}.html"
            json_filename = f"{offer_index:04d}_{timestamp}_metadata.json"
            
            html_path = issue_dir / html_filename
            json_path = issue_dir / json_filename
            
            # Save HTML
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            # Prepare metadata
            issue_metadata = {
                "timestamp": datetime.now().isoformat(),
                "issue_type": issue_type,
                "offer_index": offer_index,
                "url": url,
                "page_state": page_state,
                "html_file": str(html_path),
                "html_size_bytes": len(html_content),
                "html_line_count": len(html_content.split('\n')),
                "custom_metadata": metadata or {},
            }
            
            # Save metadata as JSON
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(issue_metadata, f, indent=2)
            
            # Update statistics
            self.stats["html_dumps_saved"] += 1
            self.stats["issues_logged"].append({
                "type": issue_type,
                "offer_index": offer_index,
                "timestamp": datetime.now().isoformat(),
                "html_file": html_filename,
                "metadata_file": json_filename,
            })
            
            return str(html_path)
        
        except Exception as e:
            logging.error(f"Failed to save HTML for issue: {e}")
            return None
    
    def save_session_stats(self):
        """Save session statistics to file"""
        try:
            self.stats["end_time"] = datetime.now().isoformat()
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save session stats: {e}")


def setup_comprehensive_logging(
    log_name: str = None,
    log_level: int = logging.INFO,
    include_html_debugging: bool = True
) -> tuple:
    """
    Set up comprehensive logging with file and console output, plus optional HTML debugging.
    
    Args:
        log_name: Name for the log files (default: auto-generated from timestamp)
        log_level: Logging level (default: INFO)
        include_html_debugging: Whether to create HTML debugging handler
        
    Returns:
        Tuple of (logger, debug_handler)
    """
    
    # Create log directory
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Generate log filename if not provided
    if log_name is None:
        log_name = f"scraper_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    log_file = log_dir / f"{log_name}.log"
    
    # Remove existing handlers
    logger = logging.getLogger(__name__)
    logger.handlers.clear()
    logger.setLevel(log_level)
    
    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    colored_formatter = IssueDumpFormatter(
        '%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # File handler - detailed logs
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(detailed_formatter)
    logger.addHandler(file_handler)
    
    # Console handler - colored output
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(colored_formatter)
    logger.addHandler(console_handler)
    
    # Initialize HTML debugging handler if requested
    debug_handler = None
    if include_html_debugging:
        debug_handler = DebugHTMLHandler()
        logger.info(f"🔍 Debug session created: {debug_handler.session_id}")
        logger.info(f"📁 Debug directory: {debug_handler.session_dir}")
    
    logger.info(f"✅ Logging initialized: {log_file}")
    
    return logger, debug_handler


def log_extraction_attempt(
    logger: logging.Logger,
    offer_index: int,
    source: str,
    url: str,
    selectors_to_test: Dict[str, list],
    page_state: str = None
):
    """
    Log the start of an extraction attempt with details.
    
    Args:
        logger: Logger instance
        offer_index: Index of offer being extracted
        source: Source (OLX/Otodom)
        url: URL being extracted
        selectors_to_test: Dictionary of selectors being tested
        page_state: Description of page state
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"🔄 EXTRACTION ATTEMPT - {source} Offer #{offer_index}")
    logger.info(f"{'='*80}")
    logger.info(f"URL: {url}")
    
    if page_state:
        logger.info(f"Page State: {page_state}")
    
    logger.info(f"Selectors to test:")
    for field, selectors in selectors_to_test.items():
        logger.info(f"  • {field}: {len(selectors)} selector(s)")
        for i, sel in enumerate(selectors[:2], 1):  # Show first 2 selectors
            logger.debug(f"    [{i}] {sel}")


def log_selector_attempt(
    logger: logging.Logger,
    field: str,
    selector: str,
    result: str,
    attempt_num: int = None,
    total_attempts: int = None
):
    """
    Log selector matching attempt with result.
    
    Args:
        logger: Logger instance
        field: Field name being extracted
        selector: CSS selector being tested
        result: Result of attempt ('found', 'not_found', 'error')
        attempt_num: Current attempt number
        total_attempts: Total attempts for this field
    """
    attempt_str = f"[{attempt_num}/{total_attempts}] " if attempt_num and total_attempts else ""
    
    if result == 'found':
        logger.debug(f"  ✅ {field} {attempt_str}Found via selector")
        logger.debug(f"     Selector: {selector[:70]}{'...' if len(selector) > 70 else ''}")
    elif result == 'not_found':
        logger.debug(f"  ❌ {field} {attempt_str}Not found with selector")
        logger.debug(f"     Selector: {selector[:70]}{'...' if len(selector) > 70 else ''}")
    elif result == 'error':
        logger.debug(f"  ⚠️  {field} {attempt_str}Error with selector")
        logger.debug(f"     Selector: {selector[:70]}{'...' if len(selector) > 70 else ''}")


def log_extraction_result(
    logger: logging.Logger,
    offer_index: int,
    source: str,
    fields_found: Dict[str, Any],
    fields_missing: list,
    success: bool,
    error_reason: str = None
):
    """
    Log the result of an extraction attempt.
    
    Args:
        logger: Logger instance
        offer_index: Index of offer
        source: Source (OLX/Otodom)
        fields_found: Dictionary of successfully extracted fields
        fields_missing: List of fields that were not found
        success: Whether extraction was overall successful
        error_reason: If failed, reason for failure
    """
    status = "✅ SUCCESS" if success else "❌ FAILED"
    logger.info(f"\n{status} - {source} Offer #{offer_index}")
    
    if fields_found:
        logger.info(f"  Extracted ({len(fields_found)} fields):")
        for field, value in fields_found.items():
            value_preview = str(value)[:50]
            if len(str(value)) > 50:
                value_preview += "..."
            logger.info(f"    • {field}: {value_preview}")
    
    if fields_missing:
        logger.info(f"  Missing ({len(fields_missing)} fields):")
        for field in fields_missing:
            logger.info(f"    • {field}")
    
    if error_reason:
        logger.warning(f"  Reason: {error_reason}")
    
    logger.info(f"{'='*80}\n")


def log_batch_summary(
    logger: logging.Logger,
    batch_num: int,
    total_offers_in_batch: int,
    successful: int,
    failed: int,
    skipped: int = 0
):
    """
    Log summary statistics for a batch of offers.
    
    Args:
        logger: Logger instance
        batch_num: Batch number
        total_offers_in_batch: Total offers in this batch
        successful: Number successfully extracted
        failed: Number that failed
        skipped: Number skipped
    """
    success_rate = (successful / total_offers_in_batch * 100) if total_offers_in_batch > 0 else 0
    
    logger.info(f"\n{'='*80}")
    logger.info(f"📊 BATCH {batch_num} SUMMARY")
    logger.info(f"{'='*80}")
    logger.info(f"Total Offers: {total_offers_in_batch}")
    logger.info(f"✅ Successful: {successful}")
    logger.info(f"❌ Failed: {failed}")
    if skipped > 0:
        logger.info(f"⏭️  Skipped: {skipped}")
    logger.info(f"📈 Success Rate: {success_rate:.1f}%")
    logger.info(f"{'='*80}\n")


def log_session_summary(
    logger: logging.Logger,
    debug_handler: 'DebugHTMLHandler',
    total_offers: int,
    successful: int,
    failed: int,
    total_time_seconds: float = None
):
    """
    Log comprehensive summary of entire session.
    
    Args:
        logger: Logger instance
        debug_handler: DebugHTMLHandler instance
        total_offers: Total offers processed
        successful: Number successfully extracted
        failed: Number that failed
        total_time_seconds: Total run time
    """
    success_rate = (successful / total_offers * 100) if total_offers > 0 else 0
    
    logger.info(f"\n{'='*80}")
    logger.info(f"📊 SESSION SUMMARY - Complete Run")
    logger.info(f"{'='*80}")
    logger.info(f"Total Offers Processed: {total_offers}")
    logger.info(f"✅ Successfully Extracted: {successful} ({success_rate:.1f}%)")
    logger.info(f"❌ Failed Extractions: {failed}")
    
    if total_time_seconds:
        logger.info(f"⏱️  Total Time: {total_time_seconds:.1f}s ({total_time_seconds/60:.1f} minutes)")
    
    if debug_handler:
        logger.info(f"\n🔍 Debug Information:")
        logger.info(f"  Session ID: {debug_handler.session_id}")
        logger.info(f"  Debug Directory: {debug_handler.session_dir}")
        logger.info(f"  HTML Dumps Saved: {debug_handler.stats['html_dumps_saved']}")
        logger.info(f"  Issues Logged: {len(debug_handler.stats['issues_logged'])}")
        
        # Save and display issue summary
        if debug_handler.stats['issues_logged']:
            logger.info(f"\n📋 Issues by Type:")
            issue_types = {}
            for issue in debug_handler.stats['issues_logged']:
                issue_type = issue['type']
                issue_types[issue_type] = issue_types.get(issue_type, 0) + 1
            
            for issue_type, count in sorted(issue_types.items()):
                logger.info(f"  • {issue_type}: {count}")
        
        # Update and save stats
        debug_handler.stats["total_offers"] = total_offers
        debug_handler.stats["successful_extractions"] = successful
        debug_handler.stats["failed_extractions"] = failed
        debug_handler.save_session_stats()
        logger.info(f"\n✅ Session stats saved to: {debug_handler.stats_file}")
    
    logger.info(f"{'='*80}\n")
