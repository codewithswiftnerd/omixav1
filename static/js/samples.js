// Tiny messy datasets for the "No file? Try one of these" tiles on the
// home page. They are built into File objects in the browser and sent
// through exactly the same upload -> report -> clean path as a real
// file, so nothing special happens server-side.
window.OMIXA_SAMPLES = {
  customers: {
    filename: 'sample_customers.csv',
    csv: [
      'Full Name,Email,Phone,Country,Gender,Signup Date,Newsletter',
      'Ada Obi, ADA@Mail.COM ,(0803) 123-4567,ng,F,"March 14, 2024",Yes',
      'Tunde Bello,tunde@mail.com,0803.123.4568,nigeria,MALE,14-Mar-2024,Y',
      'Wanjiru Kamau,wanjiru@mail.com,0712 345 678,KE,female,2024/03/09,1',
      'Wanjiru Kamau,wanjiru@mail.com,0712 345 678,KE,female,2024/03/09,1',
      'Kofi Mensah,kofi@mail.com,024-555-0192,Ghana,Male,2024-02-27,No',
      'Amina Yusuf,amina@mail,0805 555 0143,NG,f,"January 5, 2024",N',
      'Chidi Eze,CHIDI@MAIL.COM,(0902) 555-0187,Nigeria,M,05-Jan-2024,0',
      'Sara Tesfaye,sara@mail.com,0911 234 567,eth,Female,2023/12/30,TRUE',
      'Peter Otieno,peter@mail.com,0722-555-019,Kenya,male,2024-01-18,yes',
      'Grace Adeyemi,grace@mail.com,N/A,Nigeria,FEMALE,"February 2, 2024",false',
    ].join('\n'),
  },
  sales: {
    filename: 'sample_sales.csv',
    csv: [
      'Order ID,Product,Region,Units,Unit Price,Total,Order Date',
      'A-1001,Notebook,Lagos,12,"$4.50","$54.00","March 3, 2024"',
      'A-1002,Pen set,Abuja,30,"$2.00","$60.00",03-Mar-2024',
      'A-1003,Backpack,lagos,5,"$25.00","$125.00",2024/03/04',
      'A-1004,Notebook,Nairobi,N/A,"$4.50",-,2024-03-05',
      'A-1005,Desk lamp,Accra,8,"$18.00","$144.00","March 6, 2024"',
      'A-1005,Desk lamp,Accra,8,"$18.00","$144.00","March 6, 2024"',
      'A-1006,Pen set,Abuja,22,"$2.00","$44.00",07-Mar-2024',
      'A-1007,Backpack,Nairobi,3,"$25.00","$75.00",2024/03/08',
      'A-1008,Notebook,Lagos,15,"$4.50","$67.50",2024-03-09',
      'A-1009,Desk lamp,NG,6,"$18.00","$108.00","March 10, 2024"',
    ].join('\n'),
  },
  survey: {
    filename: 'sample_survey.csv',
    csv: [
      'Respondent,Age,Gender,Country,Satisfied,Would Recommend,Completed On',
      'R-01,24,F,Nigeria,Yes,Y,"April 2, 2024"',
      'R-02,31,MALE,KE,No,N,02-Apr-2024',
      'R-03,29,female,Ghana,yes,1,2024/04/03',
      'R-04,45,M,ng,Y,TRUE,2024-04-03',
      'R-05,38,Female,Kenya,N/A,0,"April 4, 2024"',
      'R-06,27,male,Nigeria,No,No,04-Apr-2024',
      'R-06,27,male,Nigeria,No,No,04-Apr-2024',
      'R-07,52,F,eth,Yes,Yes,2024/04/05',
      'R-08,33,M,GH,yes,y,2024-04-06',
      'R-09,41,Female,Nigeria,Y,N,"April 7, 2024"',
    ].join('\n'),
  },
};
