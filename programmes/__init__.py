"""Programme dataset builder for Mastersportal / Bachelorsportal pages saved as HTML.

The portals block automated pagination and detail-page access, so this package
never fetches pages itself. It imports HTML files that the user saved while
browsing normally, extracts listing cards and programme details, and maintains
per-university JSON datasets with a persistent, resumable task history.
"""
