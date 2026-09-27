"""
Timeout configuration system for web scraping operations.

This module provides configurable timeout settings optimized for web scraping,
particularly for the Kinguin scraper that experiences timeout issues.
"""

from dataclasses import dataclass


@dataclass
class TimeoutConfig:
    """
    Configuration class for timeout settings in web scraping operations.
    
    All timeout values are in milliseconds to match Playwright's timeout format.
    Default values are optimized for web scraping with AGGRESSIVE timeout detection
    and skip-on-timeout strategy to maximize speed while maintaining reliability.
    
    Attributes:
        page_load (int): Timeout for page navigation (default: 15000ms = 15 seconds)
        element_wait (int): Timeout for element detection (default: 20000ms = 20 seconds - selector wait)
        network_idle (int): Timeout for network idle state (default: 10000ms = 10 seconds - aggressive)
        retry_delay (int): Delay between retry attempts in seconds (default: 0 seconds - no delay on timeout skips)
        max_retries (int): Maximum number of retry attempts (default: 1 - skip fast on timeout)
    """
    
    page_load: int = 15000      # 15 seconds for page navigation (aggressive - skip if timeout)
    element_wait: int = 20000   # 20 seconds for element/selector detection (longer for DOM rendering)
    network_idle: int = 10000   # 10 seconds for network idle (aggressive - timeouts will be skipped)
    retry_delay: int = 0        # 0 seconds - no delay when skipping on timeout
    max_retries: int = 1        # Only 1 retry total (if not timeout), then skip
    
    def __post_init__(self):
        """Validate timeout configuration values."""
        if self.page_load <= 0:
            raise ValueError("page_load timeout must be positive")
        if self.element_wait <= 0:
            raise ValueError("element_wait timeout must be positive")
        if self.network_idle <= 0:
            raise ValueError("network_idle timeout must be positive")
        if self.retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
    
    @classmethod
    def create_skip_on_timeout_config(cls) -> 'TimeoutConfig':
        """
        Create a configuration that skips queries on timeout (maximum speed).
        
        This is the DEFAULT and RECOMMENDED config. Queries that timeout are skipped
        immediately to prevent slow cascades. This has proven to give better results
        than retrying indefinitely.
        
        Returns:
            TimeoutConfig: Configuration optimized for skip-on-timeout strategy
        """
        return cls(
            page_load=15000,    # 15 seconds - aggressive timeout
            element_wait=20000, # 20 seconds for element detection
            network_idle=10000, # 10 seconds - aggressive timeout
            retry_delay=0,      # 0 seconds - no delay on timeout skips
            max_retries=1       # Only 1 non-timeout retry
        )
    
    @classmethod
    def create_patient_config(cls) -> 'TimeoutConfig':
        """
        Create a patient configuration for very slow networks (LEGACY - not recommended).
        
        Note: Testing shows this is slower than skip-on-timeout strategy.
        Only use if skip-on-timeout causes too many false negatives.
        
        Returns:
            TimeoutConfig: Configuration with extended timeout values
        """
        return cls(
            page_load=25000,    # 25 seconds
            element_wait=20000, # 20 seconds for element detection
            network_idle=15000, # 15 seconds
            retry_delay=0,      # 0 seconds - no delay
            max_retries=1       # Only 1 retry
        )
    
    def to_dict(self) -> dict:
        """
        Convert the configuration to a dictionary.
        
        Returns:
            dict: Dictionary representation of the configuration
        """
        return {
            'page_load': self.page_load,
            'element_wait': self.element_wait,
            'network_idle': self.network_idle,
            'retry_delay': self.retry_delay,
            'max_retries': self.max_retries
        }
    
    @classmethod
    def from_dict(cls, config_dict: dict) -> 'TimeoutConfig':
        """
        Create a TimeoutConfig from a dictionary.
        
        Args:
            config_dict (dict): Dictionary containing configuration values
            
        Returns:
            TimeoutConfig: New configuration instance
        """
        return cls(**config_dict)