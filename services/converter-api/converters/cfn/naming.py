"""Names: CloudFormation identifiers to Terraform identifiers and attribute names."""

from __future__ import annotations

import re

# Words that the awscc provider keeps whole instead of splitting at the case change.
_WHOLE_WORDS = (
    ("CloudFormation", "Cloudformation"),
    ("CloudFront", "Cloudfront"),
    ("CloudWatch", "Cloudwatch"),
    ("FSx", "Fsx"),
    ("OTel", "Otel"),
)
_S3 = re.compile(r"(^|_)s_3(_|$)")
_EC2 = re.compile(r"(^|_)ec_2(_|$)")
_VERSION = re.compile(r"_v_(\d+)(_|$)")
_IDENTIFIER_INVALID = re.compile(r"[^a-z0-9_]+")
_REPEATED_UNDERSCORES = re.compile(r"_{2,}")

# Names Terraform reserves inside a variable, resource or output block.
_RESERVED = frozenset(
    {
        "count",
        "depends_on",
        "for_each",
        "lifecycle",
        "locals",
        "module",
        "provider",
        "providers",
        "source",
        "terraform",
        "version",
    }
)


def _split_words(name: str) -> str:
    """CamelCase to snake_case, the way the awscc provider does it.

    Digits are separate words ('Ipv6CidrBlock' -> 'ipv_6_cidr_block') and runs of capitals
    are one word ('DBInstanceClass' -> 'db_instance_class'). ASCII only: CloudFormation
    names are ASCII, and anything else is replaced by the caller.
    """
    out: list[str] = []
    size = len(name)
    for i, char in enumerate(name):
        is_cap = "A" <= char <= "Z"
        is_low = "a" <= char <= "z"
        is_num = "0" <= char <= "9"
        lower = char.lower() if is_cap else char
        if i + 1 < size:
            nxt = name[i + 1]
            next_cap = "A" <= nxt <= "Z"
            next_low = "a" <= nxt <= "z"
            next_num = "0" <= nxt <= "9"
            if (
                (is_cap and (next_low or next_num))
                or (is_low and (next_cap or next_num))
                or (is_num and (next_cap or next_low))
            ):
                if is_cap and next_low and i > 0 and "A" <= name[i - 1] <= "Z":
                    out.append("_")
                out.append(lower)
                if is_low or is_num or next_num:
                    out.append("_")
                continue
        out.append("_" if char in " _-.:/" else lower)
    return "".join(out)


def snake(name: str) -> str:
    """A CloudFormation property name as an awscc (and mostly aws) attribute name.

    'BucketName' -> 'bucket_name', 'VPCId' -> 'vpc_id', 'S3Bucket' -> 's3_bucket'.
    """
    for word, replacement in _WHOLE_WORDS:
        name = name.replace(word, replacement)
    result = _split_words(name)
    result = _S3.sub(r"\1s3\2", result)
    result = _EC2.sub(r"\1ec2\2", result)
    return _VERSION.sub(r"_v\1\2", result)


def word_breaks(name: str) -> str:
    """`_` where a new word starts, in one pass. Unlike snake(), digits stay with their
    word: 'S3Endpoint' -> 'S3_Endpoint', 'PublicSubnet1' -> 'Public_Subnet1',
    'DBInstance' -> 'DB_Instance'."""
    out: list[str] = []
    for index, char in enumerate(name):
        if index and char.isupper():
            previous = name[index - 1]
            following = name[index + 1] if index + 1 < len(name) else ""
            if previous.islower() or previous.isdigit():
                out.append("_")
            elif previous.isupper() and following.islower():
                out.append("_")
        out.append(char)
    return "".join(out)


def identifier(name: str, fallback: str = "this") -> str:
    """A valid Terraform block label from any CloudFormation name.

    'MyBucket' -> 'my_bucket', '1stQueue' -> 'r_1st_queue'. Never empty, never reserved.
    """
    words = _IDENTIFIER_INVALID.sub("_", word_breaks(str(name)).lower())
    words = _REPEATED_UNDERSCORES.sub("_", words).strip("_")
    if not words:
        words = fallback
    if words[0].isdigit():
        words = f"r_{words}"
    words = words[:63].rstrip("_")
    return f"{words}_" if words in _RESERVED else words


class UniqueNames:
    """Hands out identifiers, adding _2, _3... when two names collide."""

    def __init__(self) -> None:
        """No names taken yet."""
        self._used: set[str] = set()
        # Next suffix to try per wanted name, so many collisions stay linear.
        self._next: dict[str, int] = {}

    def take(self, wanted: str) -> str:
        """`wanted`, or `wanted_2`, `wanted_3`... when it is taken already."""
        name = wanted
        counter = self._next.get(wanted, 2)
        while name in self._used:
            name = f"{wanted}_{counter}"
            counter += 1
        self._next[wanted] = counter
        self._used.add(name)
        return name

    def reserve(self, name: str) -> None:
        """Mark `name` as taken without handing it out."""
        self._used.add(name)
