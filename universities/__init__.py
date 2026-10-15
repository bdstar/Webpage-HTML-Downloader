"""University profile importer for Mastersportal / Bachelorsportal pages saved as HTML.

Like the programme importer, this package never fetches pages itself. It reads
university profile pages the user saved while browsing normally, extracts the
profile facts, and writes one JSON file per university with a persistent,
resumable task history.
"""
