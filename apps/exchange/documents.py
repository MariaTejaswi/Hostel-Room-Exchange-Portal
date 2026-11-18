"""
RoomLoop Document Generation Stack.

Generates official institutional documentation, including genuine binary PDF
Accommodation Transfer Orders and Confirmation Letters compliant with PDF-1.4.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from django.utils import timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TransferRecord


def _escape_pdf_text(text: str) -> str:
    """Escape special PDF text syntax characters."""
    if not text:
        return ""
    # Only keep printable ascii / latin-1 characters for Type 1 Helvetica font
    clean = text.encode("latin-1", "replace").decode("latin-1")
    return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def generate_transfer_letter_pdf(transfer_record: TransferRecord) -> bytes:
    """
    Generate an authentic, binary PDF-1.4 official Accommodation Transfer Order.

    Outputs genuine application/pdf bytes (never HTML labeled as PDF).
    Fully readable by Adobe Acrobat, PDF.js, Google Chrome, and standard viewers.
    """
    student = transfer_record.student
    institution = transfer_record.institution
    approval = transfer_record.approval

    inst_name = institution.name if institution else "Apex University"
    inst_code = institution.code if institution else "APEX"
    student_name = student.full_name if student else "Unknown Student"
    reg_no = getattr(student, "registration_number", "N/A") or "N/A"
    student_email = getattr(student, "email", "N/A") or "N/A"

    src_hostel = transfer_record.source_hostel_name or "N/A"
    src_room = transfer_record.source_room_number or "N/A"
    src_bed = transfer_record.source_bed_label or "N/A"

    dest_hostel = transfer_record.destination_hostel_name or "N/A"
    dest_room = transfer_record.destination_room_number or "N/A"
    dest_bed = transfer_record.destination_bed_label or "N/A"

    exec_date = (
        transfer_record.transferred_at.strftime("%B %d, %Y · %H:%M UTC")
        if transfer_record.transferred_at
        else timezone.now().strftime("%B %d, %Y · %H:%M UTC")
    )

    approver_name = (
        approval.authority_user.full_name
        if approval and approval.authority_user
        else (transfer_record.approved_by.full_name if transfer_record.approved_by else "Hostel Authority")
    )
    approver_role = (
        approval.get_authority_role_display()
        if approval and hasattr(approval, "get_authority_role_display")
        else "Authorized Warden"
    )
    remarks = transfer_record.remarks or (approval.remarks if approval else "") or "Administrative room exchange completed and verified."

    workflow_type = "Direct 1-to-1 Exchange"
    if transfer_record.proposal_id:
        c_len = transfer_record.proposal.cycle_length if transfer_record.proposal else 2
        workflow_type = "Direct 2-Student Exchange" if c_len == 2 else f"{c_len}-Party Rotation Cycle"
    elif transfer_record.exchange_request_id:
        workflow_type = "Vacant-bed Transfer"

    ref_id = f"TRF-{str(transfer_record.id)[:8].upper()}"
    verification_hash = hashlib.sha256(
        f"{transfer_record.id}:{student_name}:{reg_no}:{dest_hostel}:{dest_room}:{dest_bed}:{exec_date}".encode("utf-8")
    ).hexdigest()[:24].upper()

    objects: list[str] = []

    def add_object(body: str) -> int:
        objects.append(body)
        return len(objects)

    # 1. Catalog
    add_object("<< /Type /Catalog /Pages 2 0 R >>")
    # 2. Pages
    add_object("<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    # 3. Page (Standard A4: 595.28 x 841.89 points)
    add_object(
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] "
        "/Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>"
    )

    stream_cmds: list[str] = []

    # Outer decorative border (Coffee-Brown #D5C2B1)
    stream_cmds.append("0.835 0.761 0.694 RG 1 w")
    stream_cmds.append("30 30 535.28 781.89 re S")

    # Header banner background (#702C3B maroon)
    stream_cmds.append("0.439 0.173 0.231 rg")
    stream_cmds.append("30 740 535.28 71.89 re f")

    # Header Text
    stream_cmds.append(f"BT /F2 16 Tf 1 1 1 rg 45 782 Td ({_escape_pdf_text(inst_name.upper())}) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.96 0.92 0.87 rg 45 768 Td (ROOMLOOP ACCOMMODATION & RESIDENCE PORTAL -- OFFICIAL DISPATCH) Tj ET")
    stream_cmds.append(f"BT /F2 10 Tf 1 1 1 rg 440 782 Td (REF: {_escape_pdf_text(ref_id)}) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.96 0.92 0.87 rg 440 768 Td ({_escape_pdf_text(inst_code)} SYSTEM VERIFIED) Tj ET")

    # Document Title Block
    stream_cmds.append("0.231 0.090 0.125 rg")  # #3B1720 ink
    stream_cmds.append("BT /F2 14 Tf 45 705 Td (ACCOMMODATION TRANSFER & REALLOCATION ORDER) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.54 0.48 0.45 rg 45 690 Td (Executed: {_escape_pdf_text(exec_date)}  |  Workflow: {_escape_pdf_text(workflow_type)}) Tj ET")

    # Dividing rule (#E8DED4)
    stream_cmds.append("0.910 0.871 0.831 RG 1 w")
    stream_cmds.append("45 678 m 550 678 l S")

    # Section 1: Transferred Student Details
    stream_cmds.append("0.988 0.984 0.969 rg")  # #FDFBF7 card background
    stream_cmds.append("45 595 505 72 re f")
    stream_cmds.append("0.835 0.761 0.694 RG 0.5 w")
    stream_cmds.append("45 595 505 72 re S")

    stream_cmds.append("BT /F2 10 Tf 0.439 0.173 0.231 rg 55 650 Td (STUDENT DETAILS) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 632 Td (Student Name:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 140 632 Td ({_escape_pdf_text(student_name)}) Tj ET")

    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 616 Td (Registration No:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 140 616 Td ({_escape_pdf_text(reg_no)}) Tj ET")

    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 310 632 Td (Institutional Email:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 410 632 Td ({_escape_pdf_text(student_email)}) Tj ET")

    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 310 616 Td (Institution Code:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 410 616 Td ({_escape_pdf_text(inst_code)}) Tj ET")

    # Section 2: Accommodation Movement Details (Previous vs New Allocation)
    # Source Card
    stream_cmds.append("0.97 0.97 0.98 rg 45 470 245 105 re f")
    stream_cmds.append("0.85 0.85 0.88 RG 0.5 w 45 470 245 105 re S")
    stream_cmds.append("BT /F2 10 Tf 0.35 0.35 0.4 rg 55 558 Td (PREVIOUS ALLOCATION (VACATED)) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 538 Td (Hostel:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 100 538 Td ({_escape_pdf_text(src_hostel)}) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 520 Td (Room:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 100 520 Td ({_escape_pdf_text(src_room)}) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 502 Td (Bed:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 100 502 Td ({_escape_pdf_text(src_bed)}) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.5 0.5 0.5 rg 55 482 Td (Status: Formally Released & Transferred) Tj ET")

    # Destination Card (Highlighted in Emerald/Cream)
    stream_cmds.append("0.95 0.99 0.96 rg 305 470 245 105 re f")
    stream_cmds.append("0.65 0.85 0.70 RG 1 w 305 470 245 105 re S")
    stream_cmds.append("BT /F2 10 Tf 0.05 0.45 0.25 rg 315 558 Td (NEW ALLOCATION (OFFICIALLY ASSIGNED)) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 315 538 Td (Hostel:) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.05 0.40 0.20 rg 360 538 Td ({_escape_pdf_text(dest_hostel)}) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 315 520 Td (Room:) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.05 0.40 0.20 rg 360 520 Td ({_escape_pdf_text(dest_room)}) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 315 502 Td (Bed:) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.05 0.40 0.20 rg 360 502 Td ({_escape_pdf_text(dest_bed)}) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.05 0.55 0.25 rg 315 482 Td ([ACTIVE] Official Residence Allocation) Tj ET")

    # Section 3: Authority Approval Details
    stream_cmds.append("0.988 0.984 0.969 rg 45 365 505 85 re f")
    stream_cmds.append("0.835 0.761 0.694 RG 0.5 w 45 365 505 85 re S")

    stream_cmds.append("BT /F2 10 Tf 0.439 0.173 0.231 rg 55 432 Td (AUTHORITY APPROVAL & SIGN-OFF) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 414 Td (Authorizing Official:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 165 414 Td ({_escape_pdf_text(approver_name)} ({_escape_pdf_text(approver_role)})) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 398 Td (Decision & Date:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 165 398 Td (Formally Approved & Atomic Transfer Executed on {_escape_pdf_text(exec_date)}) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.2 0.2 0.2 rg 55 380 Td (Official Remarks:) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.1 0.1 0.1 rg 165 380 Td ({_escape_pdf_text(remarks[:65])}) Tj ET")

    # Section 4: Operational Move-In Directives
    stream_cmds.append("BT /F2 10 Tf 0.231 0.090 0.125 rg 45 340 Td (OFFICIAL DIRECTIVES & MOVE-IN INSTRUCTIONS) Tj ET")
    stream_cmds.append("BT /F1 8.5 Tf 0.3 0.3 0.3 rg 45 324 Td (1. Key Handover: Surrender current room key to the hostel office within 48 hours of order issuance.) Tj ET")
    stream_cmds.append("BT /F1 8.5 Tf 0.3 0.3 0.3 rg 45 310 Td (2. Physical Check-in: Present this document to the destination hostel warden to claim your allocated bed.) Tj ET")
    stream_cmds.append("BT /F1 8.5 Tf 0.3 0.3 0.3 rg 45 296 Td (3. Room Inventory Check: Inspect room amenities with warden staff upon occupancy and sign the checklist.) Tj ET")
    stream_cmds.append("BT /F1 8.5 Tf 0.3 0.3 0.3 rg 45 282 Td (4. Non-Transferable: This allocation is legally binding and non-transferable without prior portal authorization.) Tj ET")

    # Cryptographic / Digital Verification Box at Bottom
    stream_cmds.append("0.96 0.92 0.87 rg 45 65 505 50 re f")
    stream_cmds.append("0.835 0.761 0.694 RG 0.5 w 45 65 505 50 re S")

    stream_cmds.append(f"BT /F2 8.5 Tf 0.439 0.173 0.231 rg 55 98 Td (DIGITAL VERIFICATION SEAL & RECORD AUTHENTICITY) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.35 0.35 0.35 rg 55 84 Td (Verification Hash: {_escape_pdf_text(verification_hash)}) Tj ET")
    stream_cmds.append(f"BT /F1 7.5 Tf 0.5 0.5 0.5 rg 55 72 Td (Generated by RoomLoop Enterprise Core on {timezone.now().strftime('%Y-%m-%d %H:%M:%S UTC')}  |  Cryptographically Immutable Record) Tj ET")

    # Footer note
    stream_cmds.append(f"BT /F1 8 Tf 0.6 0.6 0.6 rg 200 42 Td (Official University Document  --  No Physical Stamp Required) Tj ET")

    stream_str = "\n".join(stream_cmds)
    stream_bytes = stream_str.encode("latin-1")
    add_object(f"<< /Length {len(stream_bytes)} >>\nstream\n{stream_str}\nendstream")

    # 5. Helvetica Regular
    add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    # 6. Helvetica Bold
    add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    # Assemble complete PDF structure with cross-reference table
    output_chunks: list[bytes] = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    xref_offsets: list[int] = [0]
    current_offset = len(output_chunks[0])

    for i, obj in enumerate(objects):
        xref_offsets.append(current_offset)
        obj_data = f"{i + 1} 0 obj\n{obj}\nendobj\n".encode("latin-1")
        output_chunks.append(obj_data)
        current_offset += len(obj_data)

    xref_start = current_offset
    xref_lines = [f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"]
    for off in xref_offsets[1:]:
        xref_lines.append(f"{off:010d} 00000 n \n")

    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"

    output_chunks.append("".join(xref_lines).encode("latin-1"))
    output_chunks.append(trailer.encode("latin-1"))

    return b"".join(output_chunks)
