"""Blockchain adapters (TON).

Everything in this package is **read-only** with respect to user funds: the
bot builds unsigned message bodies that the user's own wallet signs, and reads
the escrow contract from a public API.  No private key ever enters the
process.
"""

# El paquete no re-exporta nada (Fase 7): los consumidores importan el
# submódulo concreto (`bot.chain.bodies`, `bot.chain.indexer`, …). Una fachada
# con 25 nombres solo servía para que `vulture` no viera el código muerto.
