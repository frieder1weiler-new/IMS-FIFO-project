from datetime import datetime
from typing import List, Dict, Any, Optional

class Pallet:
    # We use Optional[str] so Python knows None is a valid default value
    def __init__(self, batch_id: str, part_type: str, quantity: int, timestamp: Optional[str] = None, fifo_number: Optional[str] = None):
        self.batch_id = batch_id          # Unique identifier from SAP scan
        self.part_type = part_type        # "housing" or "cover"
        self.quantity = quantity          # Pcs on this specific pallet
        self.timestamp = timestamp or datetime.now().isoformat()
        self.fifo_number = str(fifo_number).strip() if fifo_number else None  # Physical label ID

    def to_dict(self) -> Dict[str, Any]:
        """Converts the Pallet object to a dictionary for JSON serialization."""
        result = {
            "batch_id": self.batch_id,
            "part_type": self.part_type,
            "quantity": self.quantity,
            "timestamp": self.timestamp
        }
        if self.fifo_number:
            result["fifo_number"] = self.fifo_number
        return result


class ProductStock:
    def __init__(self, 
                 product_id: str, 
                 name: str, 
                 housings: Optional[List[Dict[str, Any]]] = None, 
                 covers: Optional[List[Dict[str, Any]]] = None):
        
        self.product_id = product_id
        self.name = name
        
        # FIX: Safe extraction instead of direct ** unpacking
        self.housings = [
            Pallet(
                batch_id=h["batch_id"],
                part_type=h["part_type"],
                quantity=h["quantity"],
                timestamp=h.get("timestamp"),
                fifo_number=h.get("fifo_number")  # Safely returns None if missing
            ) 
            for h in (housings or [])
        ]
        
        self.covers = [
            Pallet(
                batch_id=c["batch_id"],
                part_type=c["part_type"],
                quantity=c["quantity"],
                timestamp=c.get("timestamp"),
                fifo_number=c.get("fifo_number")  # Safely returns None if missing
            ) 
            for c in (covers or [])
        ]

    @property
    def total_housing_pcs(self) -> int:
        """Calculates total individual housing pieces in stock."""
        return sum(pallet.quantity for pallet in self.housings)

    @property
    def total_cover_pcs(self) -> int:
        """Calculates total individual cover pieces in stock."""
        return sum(pallet.quantity for pallet in self.covers)

    @property
    def ready_assembly_sets(self) -> int:
        """Returns the maximum 1:1 pairs that can actually be built."""
        return min(self.total_housing_pcs, self.total_cover_pcs)

    def to_dict(self) -> Dict[str, Any]:
        """Converts the entire product stock profile back to JSON format."""
        return {
            "name": self.name,
            "stock": {
                "housings": [p.to_dict() for p in self.housings],
                "covers": [p.to_dict() for p in self.covers]
            }
        }