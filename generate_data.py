import sqlite3
import random

categories = ["мясная продукция", "упаковка", "молочные продукты", "бакалея", "напитки", "кондитерские изделия"]
cities = ["Москва", "Санкт-Петербург", "Казань", "Екатеринбург", "Новосибирск", "Краснодар"]
regions = ["Московская область", "Ленинградская область", "Татарстан", "Свердловская область", "Новосибирская область", "Краснодарский край"]

def generate_mock_data():
    conn = sqlite3.connect('suppliers.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            city TEXT NOT NULL,
            region TEXT NOT NULL,
            min_order INTEGER,
            price_info TEXT,
            has_delivery BOOLEAN,
            has_certificates BOOLEAN,
            contacts TEXT,
            website TEXT
        )
    ''')
    
    cursor.execute('DELETE FROM suppliers')

    suppliers = []
    for i in range(150):
        cat = random.choice(categories)
        city = random.choice(cities)
        region = random.choice(regions)
        min_order = random.choice([100, 200, 500, 1000, 5000])
        price = f"от {random.randint(100, 5000)} руб./кг"
        delivery = random.choice([True, False])
        certs = random.choice([True, True, True, False]) # Чаще есть, чем нет
        
        name = f"ООО 'Поставщик-{i+1}'"
        contacts = f"+7 (999) {random.randint(100,999)}-{random.randint(10,99)}-{random.randint(10,99)}"
        website = f"https://supplier{i+1}.ru"
        
        suppliers.append((name, cat, city, region, min_order, price, delivery, certs, contacts, website))

    cursor.executemany('''
        INSERT INTO suppliers (name, category, city, region, min_order, price_info, has_delivery, has_certificates, contacts, website)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', suppliers)

    conn.commit()
    conn.close()
    print("База данных suppliers.db успешно создана и заполнена 150 записями.")

if __name__ == "__main__":
    generate_mock_data()