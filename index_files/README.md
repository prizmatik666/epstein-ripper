this is where i'll be including my personal "complete"(at least, nearest completeness that I can confirm) .

The way doj paginates/serves .pdf filenames is crazy. you can't go by the page# for anything solid. things make no sense.

but i have checked that these include no duplicate filenames.

if you wish to use these index files for your downloads instead of doing your own scans - just place them, same filename, inside the appropriate data#/ directory and the ripper will use that as it's index.

BUT! - if you've already started downloading files for that dataset, run the index_repair.py and it will flip downloaded=false to true for files already existing locally.

check out the discussion page for more info:
https://github.com/prizmatik666/epstein-ripper/discussions/1