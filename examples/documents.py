import arrow
import decimal
import random
import uuid
from bloop import (
    Column, DateTime, Engine, Float, Integer, Map,
    String, TypedMap, UUID, new_base)


# ================================================
# Model setup
# ================================================

Base = new_base()
Product = Map(**{
    'Name': String,
    'Rating': Float,
    'Updated': DateTime('US/Pacific'),
    'Description': Map(**{
        'Title': String,
        'Body': String
    }),
    'Sellers': TypedMap(Integer)
})


class Item(Base):
    id = Column(UUID, hash_key=True)
    data = Column(Product)

engine = Engine()
engine.bind(base=Base)


# ================================================
# Usage
# ================================================

item = Item(id=uuid.uuid4())
item.data = {
    'Name': 'item-name',
    'Rating': decimal.Decimal(str(random.random())),
    'Updated': arrow.now(),
    'Description': {
        'Title': 'item-title',
        'Body': 'item-body',
    },
    'Sellers': {}
}

for i in range(4):
    seller_name = 'seller-{}'.format(i)
    item.data['Sellers'][seller_name] = random.randint(0, 100)

engine.save(item)
