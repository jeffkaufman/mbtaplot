curl 'http://webservices.nextbus.com/service/publicXMLFeed?command=routeList&a=mbta' | \
      grep "route tag" | awk -F\" '{print $2}' | while read route
do
  curl 'http://webservices.nextbus.com/service/publicXMLFeed?command=routeConfig&a=mbta&r='${route} | \
       grep "<stop tag" | grep lat | grep lon | while read line
  do 
    echo "$route $line"; 
  done
done > ugly_raw_route_table.txt

python process_route_table.py ugly_raw_route_table.txt > route_table.py
