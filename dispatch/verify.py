#!/usr/bin/env python3
"""Live verifier. Tickets close only on live proof.

For each shipped fix, watch for BOTH:
 1. the fix's proof log line in the live system, AND
 2. movement in the north-star metric the ticket targeted.

Green CI alone never closes a ticket. If the metric does not move within
the goal's verification window, the ticket reopens with the evidence.
"""
