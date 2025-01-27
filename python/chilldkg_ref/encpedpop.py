from typing import Tuple, List, NamedTuple

from secp256k1proto.secp256k1 import Scalar
from secp256k1proto.ecdh import ecdh_libsecp256k1
from secp256k1proto.keys import pubkey_gen_plain
from secp256k1proto.util import int_from_bytes

from . import simplpedpop
from .util import tagged_hash_bip_dkg, prf, InvalidContributionError


###
### Encryption
###


def ecdh(
    seckey: bytes, my_pubkey: bytes, their_pubkey: bytes, context: bytes, sending: bool
) -> Scalar:
    # TODO Decide on exact ecdh variant to use
    data = ecdh_libsecp256k1(seckey, their_pubkey)
    if sending:
        data += my_pubkey + their_pubkey
    else:
        data += their_pubkey + my_pubkey
    assert len(data) == 2 * 33 + 32
    data += context
    return Scalar(int_from_bytes(tagged_hash_bip_dkg("encpedpop ecdh", data)))


def self_pad(deckey: bytes, context_: bytes) -> Scalar:
    return Scalar(
        int_from_bytes(
            prf(seed=deckey, tag="encaps_multi self_pad", extra_input=context_)
        )
    )


def encaps_multi(
    secnonce: bytes,
    pubnonce: bytes,
    deckey: bytes,
    enckeys: List[bytes],
    context: bytes,
    idx: int,
) -> List[Scalar]:
    # This is effectively the "Hashed ElGamal" multi-recipient KEM described in
    # Section 5 of "Multi-recipient encryption, revisited" by Alexandre Pinto,
    # Bertram Poettering, Jacob C. N. Schuldt (AsiaCCS 2014). Its crucial
    # feature is to feed the index of the enckey to the hash function. The only
    # difference is that we feed also the pubnonce and context data into the
    # hash function.
    pads = []
    for i, enckey in enumerate(enckeys):
        context_ = i.to_bytes(4, byteorder="big") + context
        if i == idx:
            # We're encrypting to ourselves, so we use a symmetrically derived
            # pad to save the ECDH computation.
            pad = self_pad(deckey, context_)
        else:
            pad = ecdh(
                seckey=secnonce,
                my_pubkey=pubnonce,
                their_pubkey=enckey,
                context=context_,
                sending=True,
            )
        pads.append(pad)
    return pads


def encrypt_multi(
    secnonce: bytes,
    pubnonce: bytes,
    deckey: bytes,
    enckeys: List[bytes],
    messages: List[Scalar],
    context: bytes,
    idx: int,
) -> List[Scalar]:
    pads = encaps_multi(secnonce, pubnonce, deckey, enckeys, context, idx)
    ciphertexts = [message + pad for message, pad in zip(messages, pads, strict=True)]
    return ciphertexts


def decrypt_sum(
    deckey: bytes,
    enckey: bytes,
    pubnonces: List[bytes],
    sum_ciphertexts: Scalar,
    context: bytes,
    idx: int,
) -> Scalar:
    if idx >= len(pubnonces):
        raise IndexError
    context_ = idx.to_bytes(4, byteorder="big") + context
    sum_plaintexts = sum_ciphertexts
    for i, pubnonce in enumerate(pubnonces):
        if i == idx:
            pad = self_pad(deckey, context_)
        else:
            pad = ecdh(
                seckey=deckey,
                my_pubkey=enckey,
                their_pubkey=pubnonce,
                context=context_,
                sending=False,
            )
        sum_plaintexts = sum_plaintexts - pad
    return sum_plaintexts


###
### Messages
###


class ParticipantMsg(NamedTuple):
    simpl_pmsg: simplpedpop.ParticipantMsg
    pubnonce: bytes
    enc_shares: List[Scalar]


class CoordinatorMsg(NamedTuple):
    simpl_cmsg: simplpedpop.CoordinatorMsg
    pubnonces: List[bytes]


###
### Participant
###


class ParticipantState(NamedTuple):
    simpl_state: simplpedpop.ParticipantState
    pubnonce: bytes
    enckeys: List[bytes]
    idx: int


def serialize_enc_context(t: int, enckeys: List[bytes]) -> bytes:
    # TODO Consider hashing the result here because the string can be long, and
    # we'll feed it into hashes on multiple occasions
    return t.to_bytes(4, byteorder="big") + b"".join(enckeys)


def derive_simpl_seed(seed: bytes, pubnonce: bytes, enc_context: bytes) -> bytes:
    return prf(seed, "encpedpop seed", pubnonce + enc_context)


def participant_step1(
    seed: bytes,
    deckey: bytes,
    enckeys: List[bytes],
    t: int,
    idx: int,
    random: bytes,
) -> Tuple[ParticipantState, ParticipantMsg]:
    assert t < 2 ** (4 * 8)
    assert len(random) == 32
    n = len(enckeys)

    # Create a synthetic encryption nonce
    enc_context = serialize_enc_context(t, enckeys)
    secnonce = prf(seed, "encpodpop secnonce", random + enc_context)
    # This can be optimized: We serialize the pubnonce here, but ecdh will need
    # to deserialize it again, which involves computing a square root to obtain
    # the y coordinate.
    pubnonce = pubkey_gen_plain(secnonce)
    # Add enc_context again to the derivation of the SimplPedPop seed, just in
    # case someone derives secnonce differently.
    simpl_seed = derive_simpl_seed(seed, pubnonce, enc_context)

    simpl_state, simpl_pmsg, shares = simplpedpop.participant_step1(
        simpl_seed, t, n, idx
    )
    assert len(shares) == n

    enc_shares = encrypt_multi(
        secnonce, pubnonce, deckey, enckeys, shares, enc_context, idx
    )

    pmsg = ParticipantMsg(simpl_pmsg, pubnonce, enc_shares)
    state = ParticipantState(simpl_state, pubnonce, enckeys, idx)
    return state, pmsg


def participant_step2(
    state: ParticipantState,
    deckey: bytes,
    cmsg: CoordinatorMsg,
    enc_secshare: Scalar,
) -> Tuple[simplpedpop.DKGOutput, bytes]:
    simpl_state, pubnonce, enckeys, idx = state
    simpl_cmsg, pubnonces = cmsg

    reported_pubnonce = pubnonces[idx]
    if reported_pubnonce != pubnonce:
        raise InvalidContributionError(None, "Coordinator replied with wrong pubnonce")

    enc_context = serialize_enc_context(simpl_state.t, enckeys)
    secshare = decrypt_sum(
        deckey, enckeys[idx], pubnonces, enc_secshare, enc_context, idx
    )
    dkg_output, eq_input = simplpedpop.participant_step2(
        simpl_state, simpl_cmsg, secshare
    )
    eq_input += b"".join(enckeys) + b"".join(pubnonces)
    return dkg_output, eq_input


###
### Coordinator
###


def coordinator_step(
    pmsgs: List[ParticipantMsg],
    t: int,
    enckeys: List[bytes],
) -> Tuple[CoordinatorMsg, simplpedpop.DKGOutput, bytes, List[Scalar]]:
    n = len(enckeys)
    if n != len(pmsgs):
        raise ValueError
    simpl_cmsg, dkg_output, eq_input = simplpedpop.coordinator_step(
        [pmsg.simpl_pmsg for pmsg in pmsgs], t, n
    )
    pubnonces = [pmsg.pubnonce for pmsg in pmsgs]
    for i in range(n):
        if len(pmsgs[i].enc_shares) != n:
            raise InvalidContributionError(
                i, "Participant sent enc_shares with invalid length"
            )
    enc_secshares = [
        Scalar.sum(*([pmsg.enc_shares[i] for pmsg in pmsgs])) for i in range(n)
    ]
    eq_input += b"".join(enckeys) + b"".join(pubnonces)
    # In ChillDKG, the coordinator needs to broadcast the entire enc_secshares
    # array to all participants. But in pure EncPedPop, the coordinator needs to
    # send to each participant i only their entry enc_secshares[i].
    #
    # Since broadcasting the entire array is not necessary, we don't include it
    # in encpedpop.CoordinatorMsg, but only return it as a side output, so that
    # chilldkg.coordinator_step can pick it up. Implementations of pure
    # EncPedPop will need to decide how to transmit enc_secshares[i] to
    # participant i for participant_step2(); we leave this unspecified.
    return (
        CoordinatorMsg(simpl_cmsg, pubnonces),
        dkg_output,
        eq_input,
        enc_secshares,
    )
