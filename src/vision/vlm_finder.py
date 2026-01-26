"""
VLM (Vision Language Model) based element finder.

Uses Claude or GPT-4 Vision to find UI elements on screen when OCR fails.
More reliable than OCR for:
- Text with varying backgrounds/colors
- Partial text visibility
- Complex UI layouts
"""

import base64
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)


class VLMFinder:
    """Find UI elements using Vision Language Models."""

    # DPI/coordinate transformation constants
    # The GDS2 window client area starts at screen Y=23 (title bar offset)
    # VLM returns coordinates within the screenshot, but clicks need title bar adjustment
    TITLE_BAR_OFFSET = 100  # Pixels offset for Y coordinate

    def __init__(self, provider: str = "claude", apply_dpi_transform: bool = True):
        """
        Initialize VLM finder.

        Args:
            provider: VLM provider ("claude" or "openai")
            apply_dpi_transform: Whether to apply DPI coordinate transformation
        """
        self.provider = provider.lower()
        self.apply_dpi_transform = apply_dpi_transform

        if self.provider == "claude":
            try:
                import anthropic
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    raise ValueError("ANTHROPIC_API_KEY not set")
                self.client = anthropic.Anthropic(api_key=api_key)
                self.model = "claude-sonnet-4-20250514"
                logger.info("VLM Finder initialized with Claude")
            except ImportError:
                raise ImportError("anthropic package not installed. Run: pip install anthropic")

        elif self.provider == "openai":
            try:
                import openai
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise ValueError("OPENAI_API_KEY not set")
                self.client = openai.OpenAI(api_key=api_key)
                self.model = "gpt-4o"
                logger.info("VLM Finder initialized with OpenAI")
            except ImportError:
                raise ImportError("openai package not installed. Run: pip install openai")

        else:
            raise ValueError(f"Unknown provider: {provider}. Use 'claude' or 'openai'")

    def _transform_coordinates(self, x: int, y: int) -> Tuple[int, int]:
        """
        Apply coordinate transformation to VLM-returned coordinates.

        VLM returns coordinates within the screenshot image.
        For clicking, we may need to adjust for window title bar.

        Args:
            x, y: Raw coordinates from VLM

        Returns:
            Transformed (x, y) coordinates for clicking
        """
        if not self.apply_dpi_transform:
            return (x, y)

        # Add title bar offset to Y coordinate
        actual_y = y + self.TITLE_BAR_OFFSET
        return (x, actual_y)

    def _image_to_base64(self, image: Image.Image) -> str:
        """Convert PIL Image to base64 string."""
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return base64.standard_b64encode(buffer.getvalue()).decode("utf-8")

    def find_text_position(
        self,
        target_text: str,
        screenshot: Optional[Image.Image] = None,
        apply_offset: bool = True,
    ) -> Optional[Tuple[int, int]]:
        """
        Find the position of text on screen using VLM.

        Args:
            target_text: Text to find (e.g., "Engine Data")
            screenshot: Optional screenshot, captures current screen if not provided
            apply_offset: Whether to apply Y offset transformation (default True)

        Returns:
            (x, y) center coordinates if found, None otherwise
        """
        if screenshot is None:
            screenshot = ImageGrab.grab()

        width, height = screenshot.size
        image_base64 = self._image_to_base64(screenshot)

        prompt = f"""Look at this screenshot and find the text "{target_text}".

If you find it, respond with ONLY the approximate center coordinates in this exact format:
FOUND: x, y

Where x and y are pixel coordinates from the top-left corner of the image.
The image is {width}x{height} pixels.

If you cannot find the text "{target_text}", respond with:
NOT_FOUND

Important:
- Look for exact or very close matches to "{target_text}"
- The text might be in a list, button, or menu item
- Return the CENTER of where the text appears
- Only return coordinates, no explanation needed"""

        try:
            if self.provider == "claude":
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=100,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": image_base64,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": prompt,
                                },
                            ],
                        }
                    ],
                )
                result = response.content[0].text.strip()

            elif self.provider == "openai":
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=100,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": prompt,
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{image_base64}",
                                    },
                                },
                            ],
                        }
                    ],
                )
                result = response.choices[0].message.content.strip()

            logger.debug(f"VLM response: {result}")

            # Parse response
            if result.startswith("FOUND:"):
                coords = result.replace("FOUND:", "").strip()
                raw_x, raw_y = map(int, coords.split(","))
                # Apply DPI transformation only if requested
                if apply_offset:
                    x, y = self._transform_coordinates(raw_x, raw_y)
                    logger.info(f"VLM found '{target_text}' at raw ({raw_x}, {raw_y}) -> transformed ({x}, {y})")
                else:
                    x, y = raw_x, raw_y
                    logger.info(f"VLM found '{target_text}' at ({x}, {y}) (no offset)")
                return (x, y)
            else:
                logger.warning(f"VLM could not find '{target_text}'")
                return None

        except Exception as e:
            logger.error(f"VLM error: {e}")
            return None

    def find_and_click_text(
        self,
        target_text: str,
        screenshot: Optional[Image.Image] = None,
    ) -> bool:
        """
        Find text on screen using VLM and click on it.

        Args:
            target_text: Text to find and click
            screenshot: Optional screenshot

        Returns:
            True if found and clicked, False otherwise
        """
        import pyautogui

        coords = self.find_text_position(target_text, screenshot)
        if coords:
            x, y = coords
            logger.info(f"Clicking on '{target_text}' at ({x}, {y})")
            pyautogui.click(x, y)
            return True
        return False


# Convenience function
def find_text_with_vlm(
    target_text: str,
    provider: str = "claude",
) -> Optional[Tuple[int, int]]:
    """
    Find text on screen using VLM.

    Args:
        target_text: Text to find
        provider: VLM provider ("claude" or "openai")

    Returns:
        (x, y) coordinates if found, None otherwise
    """
    finder = VLMFinder(provider=provider)
    return finder.find_text_position(target_text)
